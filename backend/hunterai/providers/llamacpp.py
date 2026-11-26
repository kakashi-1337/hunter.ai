"""Adapter for a llama.cpp server (`llama-server` of github.com/ggml-org/llama.cpp): the owner's own program, which
runs GGUF models on his machine behind an OpenAI-compatible API.

It is the OpenAI-compatible adapter with what llama-server does differently. Each point below was seen on the real
server (built from its source at 074f1f3, 2026-11-26) with the story model of its own tests, the tiny picture model of
its own tests, and Qwen3.5 0.8B; tests/llamacpp_wire.py holds what it sent.

- It is a program of the owner's, at an address he gives (Settings; URL_ENV in the .env file). Without an address
  llama.cpp is not set up, and nothing is asked anywhere. No port is assumed: it listens on 9931 since October 2026 and
  on 8080 before. It is asked directly: neither the proxy for the model providers nor one from the environment stands
  in between. A key is sent only when one is set; llama-server wants one only when it was started with --api-key.
- Two shapes. Started with a model (-m), it serves that one, and answers a request for any other name with it, so a
  chat set to a name it does not list is not sent. Started without one (a router: --models-dir), it lists the GGUF
  files of a folder and loads the one a request names before it answers; nothing is sent while it loads.
- /v1/models names each model, the context one request has (meta.n_ctx: with two slots it is half of -c), and what
  the model reads. A router says for each model whether it is loaded, and for one that is not, the command its
  instance will be started with. Nothing is assumed that the server did not say.
- An answer is a stream of OpenAI chunks: thinking as reasoning_content, a tool call as its id and name and then its
  arguments in pieces. When the server is stopped while it writes, the body breaks off under a status of 200.
- Thinking is switched off with reasoning_effort "none" and on with chat_template_kwargs.enable_thinking. No other
  effort value is sent: a chat template takes its own values and refuses the rest with a 400.
- It takes several pictures in one request, and a schema for the answer in the shape OpenAI has.
"""
from __future__ import annotations

import contextlib
import json
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .. import config
from .base import (
    ModelInfo,
    Options,
    ProviderError,
    ToolSpec,
    WireMessage,
    error_from_status,
)
from .openai_compat import OpenAICompatProvider

NAME = "llamacpp"
URL_ENV = "HUNTERAI_LLAMACPP_URL"
KEY_ENV = "LLAMACPP_API_KEY"
OPTIONS = ["temperature", "top_p", "max_tokens", "thinking"]
TIMEOUT = httpx.Timeout(900.0, connect=5.0)     # a router loads the model first, and a long request on a small card takes minutes
ASK = httpx.Timeout(5.0, connect=2.0)   # for its list of models, which it answers at once even while it writes
KEEP = 30.0                             # seconds its list of models is taken to still hold
NOT_YET = ("downloading", "incomplete")         # what a router says of a model whose file is not all there
STEP = 4096                             # a context is named in steps of this many tokens
_now = time.monotonic

_NOT_FOUND = re.compile(r"model '(.+)' not found")


def typed(written: str) -> str:
    """An address as the owner wrote it, as the address the API stands under: "http://host:port", without "/v1".
    Raises ValueError with the reason when it is none."""
    text = (written or "").strip()
    if not text:
        raise ValueError("it is empty")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("it has a space or a control character in it")
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        port = 0
    if port == 0:
        raise ValueError("its port is not a number from 1 to 65535, or it is not an address")
    if parts.scheme not in ("http", "https"):
        raise ValueError("it has to begin with http:// or https://")
    if not parts.hostname:
        raise ValueError("it names no host")
    if parts.username is not None:          # a name, a password, or both, before the host
        raise ValueError("a key does not go into the address: enter it in the field for the key")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ValueError('it has a part after "?" or "#", which the address of llama-server does not have')
    host = "127.0.0.1" if parts.hostname == "0.0.0.0" else parts.hostname
    path = parts.path.rstrip("/").removesuffix("/v1")
    return f"{parts.scheme}://" + (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "") + path


def address() -> str | None:
    """Where llama-server is, as the owner set it. None when he set no address, or what is written there is none."""
    try:
        return typed(config.env(URL_ENV) or "")
    except ValueError:
        return None


def context_for(tokens: int) -> int:
    """The context to name for `tokens`: the next STEP that holds them. llama-server takes any size and reserves the
    memory of all of it when it starts, so the size is not doubled: on a laptop card the next power of two can be
    some gigabytes that are never used."""
    return math.ceil(tokens / STEP) * STEP


def context_to_start(tokens: int, has: int) -> int:
    """The context to start llama-server with (-c) for a request of `tokens`: one that holds it and a quarter more for
    the answer, and not less than it has now."""
    return context_for(max(math.ceil(tokens * 1.25), has))


@dataclass(frozen=True)
class Held:
    """One model of the server. loaded: it is in memory. A router loads one that is not before it answers. failed: the
    router's last try to load it ended in an error."""
    info: ModelInfo
    aliases: tuple[str, ...] = ()
    loaded: bool = True
    failed: bool = False


@dataclass(frozen=True)
class Served:
    """What the server at the address says of itself. state: "off" no address is set, "down" nothing answers there,
    "loading" it is still loading its model, "key" it wants a key it was not given, "odd" what answers is not
    llama-server, "nomodel" it has no model to chat with, "ok" it serves `held`."""
    state: str
    at: str = ""
    said: str = ""                      # what was wrong, in a few words
    router: bool = False                # started without a model: it serves the models of a folder
    held: tuple[Held, ...] = ()

    @property
    def models(self) -> tuple[ModelInfo, ...]:
        return tuple(h.info for h in self.held)

    def named(self, model: str) -> Held | None:
        return next((h for h in self.held if model == h.info.id or model in h.aliases), None)


def _error(body: str) -> tuple[str, str, dict]:
    """(type, message, the whole error) as llama-server writes one; ("", "", {}) when it is written another way."""
    try:
        said = json.loads(body)
    except ValueError:
        said = None
    err = said.get("error") if isinstance(said, dict) else None
    if isinstance(err, dict):
        return str(err.get("type") or ""), str(err.get("message") or ""), err
    return "", "", {}


async def _props(client: httpx.AsyncClient, at: str, headers: dict) -> dict:
    """What /props of the server says; {} when it does not answer, or with something else."""
    try:
        said = (await client.get(f"{at}/props", headers=headers)).json()
    except (httpx.HTTPError, ValueError):
        return {}
    return said if isinstance(said, dict) else {}


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _after(args: list, flag: str) -> int | None:
    """The whole number that follows `flag` in a command; None when it is not there or is no number."""
    try:
        return int(args[args.index(flag) + 1])
    except (ValueError, IndexError, TypeError):
        return None


def _context(entry: dict) -> int | None:
    """The context one request to this model has. A loaded model says it. For one a router has not loaded, the command
    of its instance says the context it will be started with, which explicit slots share; llama-server then holds a
    request to the length the model was trained with (context_length), however large that context is."""
    meta = entry.get("meta")
    held = _number(meta.get("n_ctx")) if isinstance(meta, dict) else None
    status = entry.get("status")
    args = status.get("args") if isinstance(status, dict) else None
    if held or not isinstance(args, list):
        return held
    size, slots = _after(args, "--ctx-size"), _after(args, "--parallel")
    if not size or size < 0:
        return None
    each = size // slots if slots and slots > 1 else size
    return min(each, _number(entry.get("context_length")) or each)


def _reads_pictures(entry: dict) -> bool | None:
    """Whether the list says the model reads pictures. None when it does not say what the model reads."""
    made = entry.get("architecture")
    reads = made.get("input_modalities") if isinstance(made, dict) else None
    return "image" in reads if isinstance(reads, list) else None


def _writes_text(entry: dict) -> bool:
    """Whether a listed model answers in a chat: not one that only speaks or only decides. One that does not say what it
    writes does."""
    made = entry.get("architecture")
    writes = made.get("output_modalities") if isinstance(made, dict) else None
    return "text" in writes if isinstance(writes, list) else True


def _held(entry: dict) -> Held:
    status = entry.get("status") if isinstance(entry.get("status"), dict) else None
    aliases = entry.get("aliases")
    info = ModelInfo(id=entry["id"], label=entry["id"], options=list(OPTIONS), supports_images=bool(_reads_pictures(entry)),
                     context_window=_context(entry))
    return Held(info, tuple(a for a in aliases if isinstance(a, str)) if isinstance(aliases, list) else (),
                loaded=status is None or status.get("value") == "loaded", failed=bool(status and status.get("failed")))


def why(seen: Served) -> str:
    """Why a server that was asked serves nothing to chat with, in a few words that follow "llama.cpp: "."""
    if seen.state == "down":
        return f"nothing answers at {seen.at}"
    if seen.state == "loading":
        return "it is still loading its model"
    if seen.state == "key":
        return ("it did not accept the key that is set under Settings" if config.env(KEY_ENV)
                else "it asks for a key (it was started with --api-key): enter it under Settings")
    if seen.state == "nomodel":
        return seen.said
    return f"what answers at {seen.at} is not llama-server ({seen.said})"


def _wants_key(at: str, status: int | None = None) -> ProviderError:
    said = "did not accept the key that is set" if config.env(KEY_ENV) else "asks for a key, and none is set"
    return ProviderError("auth", f"llama-server at {at} {said}. Enter the key it was started with (--api-key) under "
                                 f"Settings, llama.cpp ({KEY_ENV}).", status, complete=True)


def _other_model(model: str, seen: Served | None, status: int | None = None) -> ProviderError:
    has = [h.info.id for h in seen.held] if seen else []
    if not has:
        now = ("which llama-server does not have now. Pick one it has in the model list of this chat (reload the page when "
               "the list is an old one).")
    elif seen.router:
        now = f"which llama-server does not have now. It has {', '.join(has)}: pick one of them in the model list of this chat."
    else:
        now = (f"and llama-server serves {has[0]} now. It would answer with that model whatever name it is asked for: "
               f"pick {has[0]} in the model list of this chat.")
    return ProviderError("bad_request", f"This chat is set to the llama.cpp model {model}, {now}", status, complete=True)


def _too_long(said: str, error: dict) -> ProviderError:
    """llama-server refused a request for its size. The words keep "context window": the engine reads from them that
    the request is to be made smaller and sent once more (context.is_overflow)."""
    used, limit = _number(error.get("n_prompt_tokens")), _number(error.get("n_ctx"))
    if not used or not limit:
        return ProviderError("bad_request", f"The request does not fit the context window of the model llama-server holds ({said})", 400)
    return ProviderError(
        "bad_request", f"The request does not fit the context window of the model llama-server holds: {used:,} tokens for a "
        f"window of {limit:,}. For more room start llama-server with a larger context: -c {context_to_start(used, limit)} (a "
        "larger context needs more memory).", 400)


class _NotLoaded(ProviderError):
    """A router that was told to load no model by itself was asked for one it has not loaded."""


class LlamaCppProvider(OpenAICompatProvider):
    timeout = TIMEOUT
    trust_env = False       # no proxy from the environment between HunterAI and a server of the owner's

    def __init__(self):
        super().__init__(NAME, "", KEY_ENV, unknown_options=list(OPTIONS))
        self._seen: tuple[float, Served] | None = None      # when the server was last asked, and what it said
        self._good: Served | None = None                    # what it said when it last listed a model

    def configured(self) -> bool:
        return address() is not None

    def _base(self) -> str:
        return f"{address() or ''}/v1"

    def _headers(self) -> dict:
        key = config.env(KEY_ENV)
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _via(self) -> None:
        return None

    async def _properties(self, client: httpx.AsyncClient, at: str) -> tuple[int | None, bool]:
        """(the context of one request, whether it reads pictures) as /props of a server with one model says them: for
        a server whose list of models does not."""
        said = await _props(client, at, self._headers())
        holds, reads = said.get("default_generation_settings"), said.get("modalities")
        return (_number(holds.get("n_ctx")) if isinstance(holds, dict) else None,
                isinstance(reads, dict) and reads.get("vision") is True)

    async def properties(self) -> dict:
        """What /props of a server with one model says now. {} for a router (its /props is the router's own), and for
        a server that serves nothing."""
        seen = await self.served()
        if seen.state != "ok" or seen.router:
            return {}
        async with httpx.AsyncClient(timeout=ASK, trust_env=False) as c:
            return await _props(c, seen.at, self._headers())

    async def has_tools(self) -> bool | None:
        """Whether the chat template of the model has tool calls: without them llama-server ignores the tools of a
        request, and says nothing. None when the server does not say (a router, an older server)."""
        caps = (await self.properties()).get("chat_template_caps")
        does = caps.get("supports_tools") if isinstance(caps, dict) else None
        return does if isinstance(does, bool) else None

    async def sleeping(self) -> bool:
        """Whether a server with one model has put it out of memory for being idle (--sleep-idle-seconds): its list
        names the model all the same, and the next request loads it again."""
        return (await self.properties()).get("is_sleeping") is True

    async def _look(self, at: str) -> Served:
        try:
            async with httpx.AsyncClient(timeout=ASK, trust_env=False) as c:
                r = await c.get(f"{at}/v1/models", headers=self._headers())
                if r.status_code == 401:
                    return Served("key", at)
                if r.status_code == 503 and "loading model" in _error(r.text)[1].lower():
                    return Served("loading", at)
                if r.status_code >= 400:
                    return Served("odd", at, f"it answers HTTP {r.status_code}")
                try:
                    data = r.json().get("data")
                except (ValueError, AttributeError):
                    data = None
                if not isinstance(data, list):
                    return Served("odd", at, "it lists no model")
                entries = [m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
                if data and not entries:
                    return Served("odd", at, "it lists no model")
                router = any(isinstance(m.get("status"), dict) for m in entries)
                ready = [m for m in entries if not (isinstance(m.get("status"), dict) and m["status"].get("value") in NOT_YET)]
                held = [_held(m) for m in ready if _writes_text(m)]
                if not held:
                    return Served("nomodel", at, _none(entries), router)
                if not router and len(ready) == 1 and (held[0].info.context_window is None or _reads_pictures(ready[0]) is None):
                    size, reads = await self._properties(c, at)     # an older server: its list says neither
                    info = held[0].info
                    held[0] = Held(info.model_copy(update={
                        "context_window": info.context_window or size,
                        "supports_images": reads if _reads_pictures(ready[0]) is None else info.supports_images}), held[0].aliases)
                return Served("ok", at, "", router, tuple(held))
        except httpx.HTTPError as e:
            return Served("down", at, type(e).__name__)

    async def served(self, fresh: bool = False) -> Served:
        """What the server says of itself. It is asked again when `fresh`, when the address changed, or when the last
        answer is older than KEEP seconds: the owner may have started it with another model or another context, and a
        router loads and unloads its models as they are asked for."""
        at = address()
        if at is None:
            return Served("off")
        held = self._seen
        if not fresh and held and held[1].at == at and _now() - held[0] < KEEP:
            return held[1]
        seen = await self._look(at)
        self._seen = (_now(), seen)
        if seen.state == "ok":
            self._good = seen
        return seen

    def note(self) -> str | None:
        """Why the list of models is empty, for the page; None when it is not, or the server was not asked."""
        seen = self._seen[1] if self._seen else None
        if seen is None or seen.state in ("ok", "off") or seen.at != address():
            return None
        return why(seen)

    async def models(self) -> list[ModelInfo]:
        return list((await self.served(fresh=True)).models)

    def _info(self, model_id: str) -> ModelInfo:
        """The model as the server last listed it, by its name or another of its names. One that it did not list reads
        text, as far as is known."""
        good = self._good
        named = good.named(model_id) if good and good.at == address() else None
        return named.info if named else ModelInfo(id=model_id, label=model_id, options=list(OPTIONS))

    async def window_for(self, model: str, options: Options) -> int | None:
        """The context window of a call: the one the server holds for the model, whatever a chat carries along. None
        when the server did not say (a model a router has not loaded yet, started without -c)."""
        await self.served()
        return self._info(model).context_window

    def _payload(self, model: str, system: str, messages: list[WireMessage], options: Options,
                 tools: list[ToolSpec] | None = None) -> dict:
        body = super()._payload(model, system, messages, options, tools)
        if options.only(self._info(model).options).thinking is True:
            body["chat_template_kwargs"] = {"enable_thinking": True}
        return body

    def _refused(self, status: int, body: str, retry_after: str | None) -> ProviderError:
        kind, said, error = _error(body)
        at = address()
        if status == 401:
            return _wants_key(at, 401)
        if kind == "exceed_context_size_error":
            self._seen = None           # the instance that refused is loaded now, and says what it holds
            return _too_long(said, error)
        if status == 503 and "loading model" in said.lower():
            return ProviderError("unavailable", f"llama-server at {at} is still loading its model. Send the message again in "
                                                "a moment.", 503, complete=True)
        found = _NOT_FOUND.search(said)
        if found:
            return _other_model(found.group(1), None, status)
        if said == "model is not loaded":
            return _NotLoaded("bad_request", said, status)      # stream() says it with the name of the model
        if status == 404:           # the address leads somewhere, and what is there has no chat endpoint
            return ProviderError("bad_request", f"What answers at {at} does not have llama-server's API (HTTP 404 for "
                                                f"{at}/v1/chat/completions). See the address under Settings, llama.cpp.", 404,
                                 complete=True)
        if "image input is not supported" in said:
            return ProviderError("bad_request", "llama-server holds this model without its projector, so the model cannot read the "
                                                "picture. Start llama-server with the projector file of the model (--mmproj "
                                                "file.gguf; in a folder for a router, a file whose name starts with mmproj beside "
                                                "the model), or send the message without the picture.", status, complete=True)
        if "Failed to load image or audio file" in said:
            return ProviderError("bad_request", "llama-server could not read a picture of this chat. Attach it again as PNG or JPEG, "
                                                "or have the look_at_image tool read it.", status, complete=True)
        return error_from_status(status, self._scrub(body), retry_after)

    def _broke_off(self) -> ProviderError:
        return ProviderError(
            "unavailable", f"llama-server at {address()} ended the answer without finishing it: it stopped or was stopped while "
            "it wrote, so what is shown is not the whole answer. The terminal it runs in says why (often the machine ran out of "
            "memory). Start it again there, then send the message again.", complete=True)

    def _unreached(self, e: httpx.HTTPError) -> ProviderError:
        if isinstance(e, httpx.ConnectError):
            return ProviderError("unavailable", f"llama.cpp does not answer at {address()} ({type(e).__name__})")
        return self._broke_off()        # it took the request and the connection broke: a body that is cut off, a reset

    def _ended(self, finished: bool) -> None:
        if not finished:
            raise self._broke_off()

    async def stream(self, model: str, system: str, messages: list[WireMessage], options: Options,
                     tools: list[ToolSpec] | None = None):
        seen = await self.served()      # what the model reads is needed to write the request
        if seen.state == "off":
            raise ProviderError("bad_request", "No address is set for llama.cpp. Set it under Settings (llama.cpp), or pick "
                                               "another model for this chat.", complete=True)
        if seen.state == "key":
            raise _wants_key(seen.at)
        if seen.state in ("ok", "nomodel") and seen.named(model) is None:
            raise _other_model(model, seen)
        # "down", "loading", "odd": whether llama-server answers is for the request itself to find out.
        try:
            async with contextlib.aclosing(super().stream(model, system, messages, options, tools)) as events:
                async for ev in events:
                    yield ev
        except ProviderError as e:
            if e.kind == "timeout":         # the adapter it builds on names the provider as the code does
                raise ProviderError("timeout", f"llama-server at {seen.at} did not answer in time") from None
            if not isinstance(e, _NotLoaded):
                raise
            raise ProviderError(
                "bad_request", f"llama-server at {seen.at} has not loaded {model}, and it was started with --no-models-autoload, so "
                f"it loads no model by itself. Load {model} on llama-server's own page ({seen.at}), or start llama-server without "
                "that switch.", e.status, complete=True) from None


def _none(entries: list[dict]) -> str:
    """Why a server that lists models has none to chat with."""
    if not entries:
        return ("it lists no model: a llama-server started without one serves the GGUF files of the folder it was given "
                "(--models-dir)")
    waits = [m["id"] for m in entries if isinstance(m.get("status"), dict) and m["status"].get("value") in NOT_YET]
    if waits and len(waits) == len(entries):
        return f"it is still downloading {', '.join(waits[:3])}"
    name = next(m["id"] for m in entries if m["id"] not in waits)
    return f"it serves {name}, which writes no text (a model for speech or for decisions): start it with a chat model"


def make() -> LlamaCppProvider:
    return LlamaCppProvider()
