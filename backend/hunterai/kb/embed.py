"""Embedders. One embedding request at a time on this machine, whatever process asks: every
request holds a file lock, because the GPU and RAM here cannot take two.

The model does not stay loaded behind its work. Ollama would keep it for five minutes after
every request, and a chat's memory recall makes one on each message: the embedder then sat in
memory beside a local chat model for the whole conversation and the machine ran hot. So a
single request unloads the model with its own answer, and a job, which makes many, keeps it
loaded while it works and unloads it when it ends."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import math
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Literal, Protocol

import httpx

from .. import config
from ..providers import ollama

EMBED_BATCH = 8
EMBED_NUM_CTX = 8192
DEFAULT_EMBEDDER = "ollama:bge-m3"
KEEP_LOADED = 0              # seconds the model stays loaded after a single answer
MAX_KEEP_LOADED = 3600
JOB_KEEP_LOADED = 300        # between the batches of a job, which unloads the model itself at its end
CPU_ENV = "HUNTERAI_EMBED_ON_CPU"
_SPEC = re.compile(r"^(ollama|fake):[A-Za-z0-9._:/-]{1,100}$")


class EmbedError(Exception):
    def __init__(self, kind: Literal["unavailable", "bad_request"], message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


def keep_loaded() -> int:
    """Seconds the model stays loaded after it answered a single request (HUNTERAI_EMBED_KEEP_LOADED;
    0, the default: it is unloaded at once)."""
    try:
        seconds = int((os.environ.get("HUNTERAI_EMBED_KEEP_LOADED") or "").strip() or KEEP_LOADED)
    except ValueError:
        return KEEP_LOADED
    return min(seconds, MAX_KEEP_LOADED) if seconds >= 0 else KEEP_LOADED


def on_cpu() -> bool:
    """Whether the owner keeps the embedding model on the processor (Settings; HUNTERAI_EMBED_ON_CPU=1). The graphics
    card then stays with the chat model: the embedder takes none of its memory, so no model has to leave the card
    for it. (An Ollama that may hold one model at a time, OLLAMA_MAX_LOADED_MODELS=1, unloads the other one all the
    same.) It is slower there, which a job that embeds many files shows. Read from the env file at every request,
    so the worker that ingests follows the setting too."""
    return (config.env(CPU_ENV) or "").strip() == "1"


class Embedder(Protocol):
    spec: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def release(self) -> None: ...


@asynccontextmanager
async def embed_lock(timeout: float | None):
    """Exclusive, across processes. `timeout` None waits for ever."""
    fd = os.open(config.home() / "embed.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        began = time.monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if timeout is not None and time.monotonic() - began >= timeout:
                    raise EmbedError("unavailable", "embedder busy") from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class OllamaEmbedder:
    """`stays_loaded`: for a job with many requests. The model then stays loaded between them, and
    whoever asked for that calls `release()` when the job ends."""

    def __init__(self, model: str, lock_timeout: float | None = None, stays_loaded: bool = False):
        self.model, self.spec, self._lock_timeout = model, f"ollama:{model}", lock_timeout
        self._stays_loaded = stays_loaded

    async def release(self) -> None:
        """Unload the model now, the way `ollama stop` does. Never an error: a model that is gone, or an
        Ollama that does not answer, has nothing left to unload."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3)) as client:
                await client.post(f"{ollama.host()}/api/generate", json={"model": self.model, "keep_alive": 0})
        except (httpx.HTTPError, OSError):
            pass

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > EMBED_BATCH:
            raise ValueError(f"at most {EMBED_BATCH} texts per request")
        body = {"model": self.model, "input": texts, "truncate": False,
                "keep_alive": JOB_KEEP_LOADED if self._stays_loaded else keep_loaded(),
                # num_gpu 0: no layer of the model on the graphics card, in Ollama's words. Unsaid: Ollama decides.
                "options": {"num_ctx": EMBED_NUM_CTX, **({"num_gpu": 0} if on_cpu() else {})}}
        async with embed_lock(self._lock_timeout):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
                    r = await client.post(f"{ollama.host()}/api/embed", json=body)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                raise EmbedError("unavailable", f"Ollama is not reachable ({type(e).__name__})") from None
        if r.status_code >= 500:
            raise EmbedError("unavailable", f"Ollama answered {r.status_code}")
        if r.status_code == 404:
            raise EmbedError("bad_request", f"Ollama does not have model {self.model}. "
                                            f"Run: ollama pull {self.model}")
        if r.status_code >= 400:
            try:
                why = str(r.json().get("error") or r.text)
            except ValueError:
                why = r.text
            raise EmbedError("bad_request", why[:300])
        try:
            vectors = r.json()["embeddings"]
        except (ValueError, KeyError, TypeError):
            raise EmbedError("bad_request", "Ollama returned no embeddings") from None
        if len(vectors) != len(texts) or len({len(v) for v in vectors}) != 1:
            raise EmbedError("bad_request", "Ollama returned a wrong number or shape of embeddings")
        return vectors


class FakeEmbedder:
    """Deterministic bag-of-words vectors for tests: texts that share words are close."""

    def __init__(self, dim: int):
        self.dim, self.spec = dim, f"fake:{dim}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dim
            for token in re.findall(r"\w+", text.lower()):
                v[int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v))
            out.append([x / norm for x in v] if norm else [1.0] + [0.0] * (self.dim - 1))
        delay = os.environ.get("HUNTERAI_FAKE_EMBED_DELAY")
        if delay:
            await asyncio.sleep(float(delay))
        log = os.environ.get("HUNTERAI_FAKE_EMBED_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                for text in texts:
                    f.write(hashlib.sha256(text.encode()).hexdigest() + "\n")
                f.flush()
                os.fsync(f.fileno())
        return out

    async def release(self) -> None:
        pass


def get(spec: str, lock_timeout: float | None = None, *, stays_loaded: bool = False) -> Embedder:
    if not _SPEC.match(spec or ""):
        raise ValueError(f"unknown embedder {spec!r}")
    kind, _, name = spec.partition(":")
    if kind == "fake":
        if os.environ.get("HUNTERAI_FAKE_EMBED") != "1" or not name.isdigit() or int(name) < 1:
            raise ValueError(f"unknown embedder {spec!r}")
        return FakeEmbedder(int(name))
    return OllamaEmbedder(name, lock_timeout, stays_loaded)


async def dimension(embedder: Embedder) -> int:
    """The only source of a collection's dimension; none is written down anywhere."""
    return len((await embedder.embed(["dimension probe"]))[0])
