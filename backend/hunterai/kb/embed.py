"""Embedders. One embedding request at a time on this machine, whatever process asks: every
request holds a file lock, because the GPU and RAM here cannot take two."""
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
_SPEC = re.compile(r"^(ollama|fake):[A-Za-z0-9._:/-]{1,100}$")


class EmbedError(Exception):
    def __init__(self, kind: Literal["unavailable", "bad_request"], message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


class Embedder(Protocol):
    spec: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


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
    def __init__(self, model: str, lock_timeout: float | None = None):
        self.model, self.spec, self._lock_timeout = model, f"ollama:{model}", lock_timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > EMBED_BATCH:
            raise ValueError(f"at most {EMBED_BATCH} texts per request")
        body = {"model": self.model, "input": texts, "truncate": False,
                "options": {"num_ctx": EMBED_NUM_CTX}}
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


def get(spec: str, lock_timeout: float | None = None) -> Embedder:
    if not _SPEC.match(spec or ""):
        raise ValueError(f"unknown embedder {spec!r}")
    kind, _, name = spec.partition(":")
    if kind == "fake":
        if os.environ.get("HUNTERAI_FAKE_EMBED") != "1" or not name.isdigit() or int(name) < 1:
            raise ValueError(f"unknown embedder {spec!r}")
        return FakeEmbedder(int(name))
    return OllamaEmbedder(name, lock_timeout)


async def dimension(embedder: Embedder) -> int:
    """The only source of a collection's dimension; none is written down anywhere."""
    return len((await embedder.embed(["dimension probe"]))[0])
