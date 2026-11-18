"""Adapter for a colibri server (github.com/JustVugg/colibri): an engine that runs large models on an ordinary
machine, holding in memory what fits there and reading the rest from storage, behind an OpenAI-compatible API.

It is the OpenAI-compatible adapter with what colibri does differently. Each point below was seen on colibri's own
server (c/openai_server.py at 0a5504a) run with a scripted engine in place of a model; tests/colibri_wire.py holds
what it sent.

- It is a program of the owner's, at an address he gives (Settings; URL_ENV in the .env file). Without an address
  colibri is not set up, and nothing is asked anywhere. It is asked directly: neither the proxy for the model
  providers nor one from the environment stands in between. A key is sent only when one is set; colibri wants one
  only when it was started with one.
- A server serves one model, with a context window settled when it started. /v1/models names the model, the window,
  and whether the model reads pictures. Nothing is assumed that the server did not say.
- It works on one request at a time and lets the others wait, five minutes unless it was started with another limit,
  and sends nothing while a request waits, not even a status. The client waits longer than that.
- An answer is a stream that ends where the connection closes. When the engine fails in the middle of one, the
  stream just stops, under a status of 200: an answer that no chunk declared finished is a failure here.
- A request holds one picture at most, and 4 MiB at most.
- Thinking is switched with `enable_thinking`. `reasoning_effort` is not sent: each family of models takes other
  values and refuses the rest. `response_format` is not sent: colibri uses a schema to write faster, not to hold
  the answer to it, and some of its engines refuse a request that has one.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .. import config
from ..types import TextBlock
from .base import ModelInfo, Options, ProviderError, ToolSpec, WireMessage, error_from_status
from .openai_compat import OpenAICompatProvider

NAME = "colibri"
URL_ENV = "HUNTERAI_COLIBRI_URL"
KEY_ENV = "COLIBRI_API_KEY"
USUAL = "http://127.0.0.1:8000"         # where colibri listens unless it was started elsewhere
OPTIONS = ["temperature", "top_p", "max_tokens", "thinking"]
QUEUE_WAIT = 300                        # seconds colibri lets a request wait for its engine (its --queue-timeout)
TIMEOUT = httpx.Timeout(QUEUE_WAIT + 60.0, connect=5.0)
ASK = httpx.Timeout(5.0, connect=2.0)   # for its list of models, which it answers at once even while it writes
KEEP = 30.0                             # seconds its list of models is taken to still hold
MAX_BODY = 4 * 1024**2                  # bytes of one request that colibri reads (MAX_BODY in its server)
CONTEXTS = (16384, 32768, 65536, 131072, 262144)       # the context sizes colibri chooses among by itself
START = "./start-here.sh"               # run in the colibri folder: it starts what is set up there
_now = time.monotonic

EARLIER = ('[an earlier image, not sent again: colibri takes one image in a request] '
           '[the look_at_image tool can describe it: image="upload:{upload}"]')
MISSING = "[an image that is no longer stored: it could not be sent]"
_SIZES = re.compile(r"maximum context length is (\d+) tokens.*?at least (\d+) tokens", re.S)


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
    if parts.username is not None or parts.password is not None:
        raise ValueError("a key does not go into the address: enter it in the field for the key")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise ValueError('it has a part after "?" or "#", which the address of colibri does not have')
    host = "127.0.0.1" if parts.hostname == "0.0.0.0" else parts.hostname
    path = parts.path.rstrip("/")
    path = path[:-len("/v1")] if path.endswith("/v1") else path
    return f"{parts.scheme}://" + (f"[{host}]" if ":" in host else host) + (f":{port}" if port else "") + path


def address() -> str | None:
    """Where colibri is, as the owner set it. None when he set no address, or what is written there is none."""
    try:
        return typed(config.env(URL_ENV) or "")
    except ValueError:
        return None


def context_for(tokens: int, above: int = 0) -> int | None:
    """The smallest context colibri offers that holds `tokens` and is larger than `above`; None when none does."""
    return next((c for c in CONTEXTS if c >= tokens and c > above), None)


def context_to_start(need: int, has: int | None) -> int | None:
    """The context to start colibri with when the one it has does not hold `need` tokens. None when it does, when it
    did not say what it has, or when it has the largest there is. The largest when nothing holds `need`: more cannot
    be had."""
    if has is None or has >= need:
        return None
    return context_for(need, has) or (CONTEXTS[-1] if CONTEXTS[-1] > has else None)


@dataclass(frozen=True)
class Served:
    """What the server at the address says of itself. state: "off" no address is set, "down" nothing answers there,
    "key" it wants a key it was not given, "refused" it refused who asked, "odd" what answers is not colibri,
    "nochat" its model does not chat, "ok" it serves `models`."""
    state: str
    at: str = ""
    said: str = ""                      # its own words, or what was wrong; the model's name for "nochat"
    models: tuple[ModelInfo, ...] = ()


def _error(body: str) -> tuple[str, str]:
    """(code, message) of an error as colibri writes it; ("", the text) when it is written another way."""
    try:
        err = json.loads(body).get("error")
        if isinstance(err, dict):
            return str(err.get("code") or ""), str(err.get("message") or "")
    except (ValueError, AttributeError):
        pass
    return "", (body or "").strip()[:300]


def _listed(entry: dict) -> ModelInfo:
    reads, limit = entry.get("input_modalities"), entry.get("context_length")
    return ModelInfo(id=entry["id"], label=entry["id"], options=list(OPTIONS),
                     supports_images=isinstance(reads, list) and "image" in reads,
                     context_window=limit if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0 else None)


def _chats(entry: dict) -> bool:
    """Whether a listed model answers in a chat: it says so, and one from before colibri said such things does."""
    does = entry.get("capabilities")
    return "chat" in does if isinstance(does, list) else True


def why(seen: Served) -> str:
    """Why a server that was asked serves nothing to chat with, in a few words that follow "colibri: "."""
    if seen.state == "down":
        return f"nothing answers at {seen.at}"
    if seen.state == "key":
        return ("it did not accept the key that is set under Settings" if config.env(KEY_ENV)
                else "it asks for a key: enter it under Settings")
    if seen.state == "refused":
        return f"it refused the request: {seen.said}"
    if seen.state == "nochat":
        return f"it serves {seen.said}, which is not a chat model"
    return f"what answers at {seen.at} is not colibri ({seen.said})"


def _wants_key(at: str, status: int | None = None) -> ProviderError:
    said = "did not accept the key that is set" if config.env(KEY_ENV) else "asks for a key, and none is set"
    return ProviderError("auth", f"colibri at {at} {said}. Enter the key it was started with (its COLI_API_KEY, or "
                                 f"--api-key) under Settings, colibri ({KEY_ENV}).", status, complete=True)


def _other_model(model: str, served: str | None) -> ProviderError:
    now = (f"and colibri serves {served} now. Pick {served} in the model list of this chat." if served else
           "which colibri does not serve now. Pick the one it serves in the model list of this chat (reload the page "
           "when the list is an old one).")
    return ProviderError("bad_request", f"This chat is set to the colibri model {model}, {now}", 404 if not served else None,
                         complete=True)


def _too_long(said: str) -> ProviderError:
    """colibri refused a request for its size. The words keep "context window": the engine reads from them that the
    request is to be made smaller and sent once more (context.is_overflow)."""
    found = _SIZES.search(said)
    if not found:
        return ProviderError("bad_request", f"The request does not fit the context window of the model colibri serves ({said})", 400)
    limit, used = int(found.group(1)), int(found.group(2))
    larger = [c for c in CONTEXTS if c > limit]
    enough = next((c for c in larger if c >= used * 1.25), larger[-1] if larger and larger[-1] >= used else None)
    more = (f"For more room start colibri with a larger context: {START} --ctx {enough} in the colibri folder (a larger context "
            "needs more memory)." if enough else "colibri offers no context that holds it.")
    return ProviderError("bad_request", "The request does not fit the context window of the model colibri serves: at least "
                                        f"{used:,} tokens for a window of {limit:,}. {more}", 400)


def _too_big(size: int, body: dict) -> str:
    pictures = sum(len(part["image_url"]["url"]) for m in body["messages"] if isinstance(m.get("content"), list)
                   for part in m["content"] if part.get("type") == "image_url")
    said = f"The request is {size / 2**20:.1f} MB, and colibri reads at most {MAX_BODY // 2**20} MB in one request."
    if pictures:
        return (f"{said} The picture in it is {pictures / 2**20:.1f} MB of that, as it has to be sent: attach a smaller copy "
                "(saved as JPEG, or cut to the part that matters), or have the look_at_image tool read it. Nothing was sent.")
    return f"{said} Continue in a new chat, or send less at once. Nothing was sent."


def _one_picture(messages: list[WireMessage]) -> list[WireMessage]:
    """colibri holds one picture for a request and refuses a request with two. The latest one that can be sent stays;
    each other one becomes a line that says so, and how the model can still have it read."""
    spots = [(i, j) for i, m in enumerate(messages) if m.role == "user" for j, b in enumerate(m.blocks) if b.type == "image"]
    if len(spots) < 2:
        return messages
    stays = next(((i, j) for i, j in reversed(spots) if messages[i].images.get(messages[i].blocks[j].upload_id) is not None), None)
    out = list(messages)
    for i in {i for i, _ in spots}:
        m = messages[i]
        blocks = [b if b.type != "image" or (i, j) == stays else
                  TextBlock(text=EARLIER.format(upload=b.upload_id) if m.images.get(b.upload_id) is not None else MISSING)
                  for j, b in enumerate(m.blocks)]
        out[i] = WireMessage(m.role, blocks, m.images, m.id)
    return out


class ColibriProvider(OpenAICompatProvider):
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

    async def _look(self, at: str) -> Served:
        try:
            async with httpx.AsyncClient(timeout=ASK, trust_env=False) as c:
                r = await c.get(f"{at}/v1/models", headers=self._headers())
        except httpx.HTTPError as e:
            return Served("down", at, type(e).__name__)
        if r.status_code == 401:
            return Served("key", at)
        if r.status_code == 403:
            return Served("refused", at, _error(r.text)[1])
        if r.status_code >= 400:
            return Served("odd", at, f"it answers HTTP {r.status_code}")
        try:
            data = r.json().get("data")
        except (ValueError, AttributeError):
            data = None
        entries = [m for m in data if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]] if isinstance(data, list) else []
        if not entries:
            return Served("odd", at, "it lists no model")
        chat = tuple(_listed(m) for m in entries if _chats(m))
        return Served("ok", at, "", chat) if chat else Served("nochat", at, entries[0]["id"])

    async def served(self, fresh: bool = False) -> Served:
        """What the server says of itself. It is asked again when `fresh`, when the address changed, or when the last
        answer is older than KEEP seconds: the owner may have started it with another model or another context."""
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
        if seen is None or seen.state == "ok" or seen.at != address():
            return None
        return why(seen)

    async def models(self) -> list[ModelInfo]:
        return list((await self.served(fresh=True)).models)

    def _info(self, model_id: str) -> ModelInfo:
        """The model as the server last listed it. One that it did not list reads text, as far as is known."""
        good = self._good
        if good and good.at == address():
            for m in good.models:
                if m.id == model_id:
                    return m
        return ModelInfo(id=model_id, label=model_id, options=list(OPTIONS))

    async def window_for(self, model: str, options: Options) -> int | None:
        """The context window of a call: the one the server holds for its model, whatever a chat carries along. None
        when the server did not say."""
        await self.served()
        return self._info(model).context_window

    def _payload(self, model: str, system: str, messages: list[WireMessage], options: Options,
                 tools: list[ToolSpec] | None = None) -> dict:
        info = self._info(model)
        body = super()._payload(model, system, _one_picture(messages) if info.supports_images else messages, options, tools)
        for name in ("reasoning_effort", "response_format"):
            body.pop(name, None)
        thinking = options.only(info.options).thinking
        if thinking is not None:
            body["enable_thinking"] = thinking
        size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))      # as the client writes it
        if size > MAX_BODY:
            raise ProviderError("bad_request", _too_big(size, body), complete=True)
        return body

    def _refused(self, status: int, body: str, retry_after: str | None) -> ProviderError:
        code, said = _error(body)
        at = address() or "its address"
        if status == 401:
            return _wants_key(at, 401)
        if status == 403:
            return ProviderError("bad_request", f"colibri at {at} refused the request: {said}", 403, complete=True)
        if status == 404 and code == "model_not_found":
            model = re.search(r"`([^`]+)`", said)
            return _other_model(model.group(1) if model else "it names", None)
        if status == 404:           # the address leads somewhere, and what is there has no chat endpoint
            return ProviderError("bad_request", f"What answers at {at} does not have colibri's API (HTTP 404 for "
                                                f"{at}/v1/chat/completions). See the address under Settings, colibri.", 404, complete=True)
        if code == "context_length_exceeded":
            return _too_long(said)
        if status == 429 and code in ("queue_full", "queue_timeout"):
            what = ("colibri's queue is full: more requests wait for its engine than it lets wait." if code == "queue_full" else
                    "colibri was busy with other requests for as long as it lets one wait (five minutes, unless it was started "
                    "with another --queue-timeout).")
            wait = error_from_status(status, "", retry_after).retry_after
            return ProviderError("rate_limit", f"{what} It works on one request at a time: send the message again when it is free, "
                                               "and let fewer subagents work at the same time (Settings) if this goes on.",
                                 429, wait, complete=True)
        return error_from_status(status, self._scrub(body), retry_after)

    def _unreached(self, e: httpx.HTTPError) -> ProviderError:
        return ProviderError("unavailable", f"colibri does not answer at {address()} ({type(e).__name__})")

    def _ended(self, finished: bool) -> None:
        if not finished:
            raise ProviderError(
                "unavailable", "colibri ended the answer without finishing it: its engine failed or was stopped while it wrote, so "
                "what is shown is not the whole answer. colibri usually starts its engine again by itself: send the message again "
                "in a moment. If it goes on, `c/coli logs` in the colibri folder says why (often the machine ran out of memory).",
                complete=True)

    async def stream(self, model: str, system: str, messages: list[WireMessage], options: Options,
                     tools: list[ToolSpec] | None = None):
        seen = await self.served()      # what the model reads and holds is needed to write the request
        if seen.state == "off":
            raise ProviderError("bad_request", "No address is set for colibri. Set it under Settings (colibri), or pick another "
                                               "model for this chat.", complete=True)
        if seen.state == "key":
            raise _wants_key(seen.at)
        if seen.state == "refused":
            raise ProviderError("bad_request", f"colibri at {seen.at} refused the request: {seen.said}", 403, complete=True)
        if seen.state == "nochat":
            raise ProviderError("bad_request", f"colibri at {seen.at} serves {seen.said}, which is not a chat model. Have it "
                                               f"serve one that is ({START} --reconfigure in the colibri folder).", complete=True)
        if seen.state == "ok" and all(m.id != model for m in seen.models):
            raise _other_model(model, seen.models[0].id)
        # "down", "odd": whether colibri answers is for the request itself to find out. A list that failed once says little.
        async with contextlib.aclosing(super().stream(model, system, messages, options, tools)) as events:
            async for ev in events:
                yield ev


def make() -> ColibriProvider:
    return ColibriProvider()
