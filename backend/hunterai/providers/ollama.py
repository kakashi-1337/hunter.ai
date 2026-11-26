"""Adapter for a local Ollama server (native /api/chat, NDJSON streaming)."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx

from .. import config
from .base import (SAMPLING, ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta, ToolCall,
                   ToolSpec, Usage, WireMessage, error_from_status, flatten_text,
                   parse_arguments, rounds, safe_call_id)

# The sampling options go to Ollama under the names they have here: it hands them on to the engine it runs
# (llm/llama_server.go of Ollama at 42ac2c0, 18 November 2026).
_BASE = ["temperature", "top_p", "max_tokens", "context_window", *SAMPLING]
OPTIONS = [*_BASE, "thinking", "effort"]
EFFORTS = ("low", "medium", "high", "max")           # the level names of Ollama that HunterAI's effort setting has
_GPT_OSS = ("low", "medium", "high")                  # what gpt-oss takes, for an Ollama too old to report it


def _levels(model: str, values: list | None) -> list[str]:
    """The thinking levels a model names, in its order, from what /api/show reports in `thinking.values`."""
    if values is None:
        return list(_GPT_OSS) if model.split("/")[-1].startswith("gpt-oss") else []
    return [v for v in values if isinstance(v, str) and v in EFFORTS]


def _think(effort: str, levels: list[str]) -> str | bool | None:
    """What to send as `think` for an effort: the level of that name, else the next one up the model has,
    else its highest. None when the model names no levels."""
    if not levels:
        return None
    if effort == "none":
        return False
    order = ["low", "medium", "high", "xhigh", "max"]
    wanted = order.index(effort)
    known = sorted(levels, key=order.index)
    return next((v for v in known if order.index(v) >= wanted), known[-1])
TIMEOUT = httpx.Timeout(300.0, connect=5.0)


# The context window of a call to a local model, in tokens. It is always sent: Ollama's own default follows the
# machine's memory (4,096 on most machines, 256,000 on a large one). HunterAI's rules and tool list alone are
# about 6,000 tokens, so the window has to be well above that; every 8,000 tokens cost about 1 GB of memory with
# an 8B model. The owner sets another size in Settings (NUM_CTX_ENV), a chat with its own "context window".
DEFAULT_NUM_CTX = 16384
MIN_NUM_CTX = 2048
MAX_NUM_CTX = 1_048_576
NUM_CTX_ENV = "HUNTERAI_OLLAMA_NUM_CTX"
# A window grows by itself when a call needs more room: doubled until the request fits, then written into the chat
# as its own. This is how large it may get that way; past it a chat is shortened as before. The owner raised the
# window of a chat by hand to this size when an answer was cut off, and no further. 0: a window stays as it is set.
DEFAULT_MOST_CTX = 131_072
MOST_CTX_ENV = "HUNTERAI_OLLAMA_MAX_CTX"
CHARS_PER_TOKEN = 3.0       # counted on the careful side, as context.py does: what is sent has to fit
IMAGE_CHARS = 6_000         # what one image is counted as
TOO_LARGE = ("The request does not fit the context window of the local model {model}: about {need:,} tokens for a "
             "window of {window:,}. Raise \"Context window for local models\" in Settings (a larger window needs "
             "more memory), or set this chat's own context window.")
NO_PICTURE = "failed to load image"     # what the program behind a model answers for a picture it cannot decode
PICTURE_REFUSED = ("Ollama could not read a picture of this chat (its words: {said}). HunterAI sends only pictures it can "
                   "decode itself, so this is one the program behind {model} does not take, and it goes with every message of "
                   "the chat. Continue in a new chat without the picture, or pick another model for this chat.")


def usable_num_ctx(written) -> int | None:
    """A window size as the owner wrote it, or None when it names no size that can be used."""
    try:
        n = int(str(written).strip())
    except (TypeError, ValueError):
        return None
    return n if MIN_NUM_CTX <= n <= MAX_NUM_CTX else None


def default_num_ctx() -> int:
    """The window of a call to a local model when the chat sets none: the owner's, else HunterAI's."""
    return usable_num_ctx(config.env(NUM_CTX_ENV)) or DEFAULT_NUM_CTX


def largest_num_ctx() -> int:
    """How large the window of a local model may get by itself: the owner's size, else HunterAI's. 0 when the owner
    switched growing off."""
    written = (config.env(MOST_CTX_ENV) or "").strip()
    return 0 if written == "0" else usable_num_ctx(written) or DEFAULT_MOST_CTX


def host() -> str:
    h = (config.env("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h.replace("0.0.0.0", "127.0.0.1")


ASK = httpx.Timeout(3.0, connect=2.0)       # for what Ollama holds in memory: it is asked before a call, not instead of one


def same_model(a: str, b: str) -> bool:
    """Whether two names mean one model: Ollama writes the tag "latest" where it was given none."""
    return a == b or a + ":latest" == b or a == b + ":latest"


async def in_memory(model: str) -> bool | None:
    """Whether Ollama holds this model in memory now, as /api/ps lists it. None when Ollama does not say: it does
    not answer, or what answers has no such list (an Ollama from before it had the route)."""
    try:
        async with httpx.AsyncClient(timeout=ASK, trust_env=False) as client:
            r = await client.get(host() + "/api/ps")
        held = r.json().get("models")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(held, list):
        return None
    return any(same_model(str(entry[key]), model) for entry in held if isinstance(entry, dict)
               for key in ("name", "model") if entry.get(key))


def _is_embedder(name: str) -> bool:
    n = name.lower()
    return "embed" in n or n.startswith("bge-")


def processor(entry: dict) -> str:
    """Where Ollama runs a loaded model, in the words of `ollama ps`, from an entry of /api/ps."""
    size, vram = int(entry.get("size") or 0), int(entry.get("size_vram") or 0)
    if vram == 0:
        return "100% CPU"
    if vram == size:
        return "100% GPU"
    if vram > size or size == 0:
        return "unknown"
    cpu = round((size - vram) / size * 100)
    return f"{cpu}%/{100 - cpu}% CPU/GPU"


def bytes_per_token(info: dict) -> int | None:
    """What one token of context window takes in memory for a model, from the `model_info` of /api/show: the key
    and the value of every head of every layer, two bytes a number, which is how Ollama keeps them unless it is
    told otherwise (OLLAMA_KV_CACHE_TYPE). Measured on Ollama 0.40.16 with qwen3:0.6b: 2 to 6 percent more than
    this. None when the model does not say enough."""
    def number(name: str) -> int | None:
        v = info.get(f"{info.get('general.architecture')}.{name}")
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None
    layers, heads = number("block_count"), number("attention.head_count")
    kv = number("attention.head_count_kv") or heads
    key = number("attention.key_length")
    if key is None:
        width = number("embedding_length")
        key = width // heads if width and heads else None
    if not (layers and kv and key):
        return None
    return layers * kv * (key + (number("attention.value_length") or key)) * 2


class OllamaProvider:
    supports_tools = True

    name = "ollama"

    def __init__(self):
        self._vision: dict[str, bool] = {}
        self._thinking: dict[str, list | None] = {}     # model -> `thinking.values`, None when Ollama reports none
        self._limit: dict[str, int | None] = {}         # model -> the most tokens it takes, None when its file does not say
        self._cost: dict[str, int | None] = {}          # model -> bytes of memory for one token of window
        self._refused: dict[str, int] = {}              # model -> the smallest window it did not answer with

    def configured(self) -> bool:
        return True

    async def supports_images(self, model: str) -> bool:
        """Whether Ollama lists the "vision" capability for this model (cached once known)."""
        if model in self._vision:
            return self._vision[model]
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(f"{host()}/api/show", json={"model": model})
                r.raise_for_status()
                shown = r.json()
                vision = "vision" in (shown.get("capabilities") or [])
                values = (shown.get("thinking") or {}).get("values") if isinstance(shown.get("thinking"), dict) else None
                info = shown.get("model_info") if isinstance(shown.get("model_info"), dict) else {}
                limit = next((v for k, v in info.items() if k.endswith(".context_length") and isinstance(v, int) and v > 0), None)
        except (httpx.HTTPError, ValueError, AttributeError):
            return False  # unknown: send no images, and ask again next time
        self._vision[model] = vision
        self._thinking[model] = values if isinstance(values, list) else None
        self._limit[model] = limit
        self._cost[model] = bytes_per_token(info)
        return vision

    async def described(self, model: str) -> tuple[int | None, int | None] | None:
        """(the most tokens the model takes, the bytes one token of window takes), as far as its file says; None
        when Ollama does not have the model or does not answer."""
        if model not in self._limit:
            await self.supports_images(model)       # one /api/show answers this too
        return (self._limit[model], self._cost.get(model)) if model in self._limit else None

    async def thinking_values(self, model: str) -> list | None:
        """`thinking.values` as /api/show reports it: booleans for a switch, names for levels, `[False]`
        for a model that does not think. None when this Ollama reports nothing, or cannot be asked."""
        if model not in self._thinking:
            await self.supports_images(model)       # one /api/show answers both questions
        return self._thinking.get(model)

    async def window_for(self, model: str, options: Options) -> int:
        """The context window a call to this model gets, in tokens, which is the `num_ctx` it is sent with: the
        chat's own, else the one for local models; never more than the model takes (Ollama would lower it by
        itself and say nothing)."""
        wanted = options.context_window or default_num_ctx()
        if model not in self._limit:
            await self.supports_images(model)       # one /api/show answers this too
        limit = self._limit.get(model)
        return min(wanted, limit) if limit else wanted

    async def larger_windows(self, model: str, options: Options) -> list[int]:
        """The windows a call to this model can be given in place of the one it gets, smallest first: that one
        doubled, and doubled again, up to the largest a window may grow to by itself and never past what the model
        takes. Empty when there is none: growing is switched off, the window is that large already, or the model
        did not answer with the next size (too_large)."""
        now = await self.window_for(model, options)
        most = min(largest_num_ctx(), self._limit.get(model) or MAX_NUM_CTX)
        out, size = [], now * 2
        while size < most:
            out.append(size)
            size *= 2
        refused = self._refused.get(model)
        return [n for n in (out + [most] if most > now else []) if not refused or n < refused]

    def too_large(self, model: str, size: int) -> None:
        """This model did not answer with a window of `size` tokens and does with a smaller one (the machine has not
        the memory for it): larger_windows() offers nothing that large again, for as long as HunterAI runs."""
        self._refused[model] = min(size, self._refused.get(model) or size)

    def _options(self, model: str) -> tuple[list[str], list[str]]:
        values = self._thinking.get(model)
        levels = _levels(model, values)
        # A switch where thinking can be turned on or off; `[False]` alone is a model that does not think.
        switch = values is None or True in values or (False in values and len(values) > 1)
        return _BASE + (["thinking"] if switch else []) + (["effort"] if levels else []), levels

    async def models(self) -> list[ModelInfo]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{host()}/api/tags")
                r.raise_for_status()
                names = [m["name"] for m in r.json().get("models", []) if m.get("name")]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return []
        names = [n for n in names if not _is_embedder(n)]
        vision = await asyncio.gather(*(self.supports_images(n) for n in names))
        return [ModelInfo(id=n, label=n, options=self._options(n)[0], supports_images=v, efforts=self._options(n)[1],
                          context_window=await self.window_for(n, Options()))
                for n, v in zip(names, vision)]

    @staticmethod
    def _assistant(m: WireMessage) -> list[dict]:
        """An assistant message round by round, in the shape Ollama's /api/chat documents:
        arguments as an object, results keyed by tool name."""
        out: list[dict] = []
        for r in rounds(m):
            calls = [b for b in r.blocks if b.type == "tool_call"]
            entry: dict = {"role": "assistant", "content": "\n\n".join(
                b.text for b in r.blocks if b.type == "text" and b.text)}
            if calls:
                entry["tool_calls"] = [
                    {"type": "function",
                     "function": {"index": i, "name": c.name, "arguments": c.arguments}}
                    for i, c in enumerate(calls)]
            out.append(entry)
            names = {c.id: c.name for c in calls}
            out += [{"role": "tool", "tool_name": names[res.tool_call_id], "content": res.content}
                    for res in r.results]
        return out

    async def stream(self, model: str, system: str, messages: list[WireMessage],
                     options: Options, tools: list[ToolSpec] | None = None):
        o = options.only(OPTIONS)
        num_ctx = await self.window_for(model, o)
        opts = {k: v for k, v in (("num_ctx", num_ctx), ("num_predict", o.max_tokens),
                                  ("temperature", o.temperature), ("top_p", o.top_p),
                                  *((name, getattr(o, name)) for name in SAMPLING))
                if v is not None}
        # truncate, shift: left to itself, Ollama removes the oldest messages of a request that is larger than the
        # window, then tokens from its middle, and answers as if nothing had happened. Told not to, it refuses.
        body: dict = {"model": model, "stream": True, "truncate": False, "shift": False, "options": opts, "messages": []}
        if system:
            body["messages"].append({"role": "system", "content": system})
        has_images = any(b.type == "image" for m in messages for b in m.blocks)
        images_ok = has_images and await self.supports_images(model)
        for m in messages:
            if m.role == "assistant" and any(b.type == "tool_call" for b in m.blocks):
                body["messages"] += self._assistant(m)
                continue
            entry: dict = {"role": m.role, "content": flatten_text(m, images_ok=images_ok)}
            if images_ok and m.role == "user":
                data = [base64.b64encode(m.images[b.upload_id]).decode() for b in m.blocks
                        if b.type == "image" and b.upload_id in m.images]
                if data:
                    entry["images"] = data
            body["messages"].append(entry)
        if o.json_schema is not None:
            body["format"] = o.json_schema      # constrained decoding
        # Thinking: off is off, an effort is the level of that name where the model has levels, on is on.
        think: str | bool | None = o.thinking
        if o.effort is not None and o.thinking is not False:
            level = _think(o.effort, _levels(model, await self.thinking_values(model)))
            think = level if level is not None else think
        if think is not None:
            body["think"] = think
        if tools:
            body["tools"] = [{"type": "function",
                              "function": {"name": t.name, "description": t.description,
                                           "parameters": t.parameters}} for t in tools]
        # An older Ollama does not know those two switches and cuts all the same, so what cannot fit is not sent.
        need = int(_chars(body) / CHARS_PER_TOKEN)
        if need > num_ctx:
            raise ProviderError("bad_request", TOO_LARGE.format(model=model, need=need, window=num_ctx), complete=True)
        taken: set[str] = set()
        usage, reason = Usage(0, 0), "end"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                async with client.stream("POST", f"{host()}/api/chat", json=body) as r:
                    if r.status_code == 404:
                        raise ProviderError(
                            "bad_request",
                            f"Ollama does not have model {model}. Run: ollama pull {model}", 404, complete=True)
                    if r.status_code >= 400:
                        text = (await r.aread()).decode("utf-8", "replace")
                        if r.status_code == 400 and "does not support tools" in text:
                            raise ProviderError(
                                "bad_request", f"Ollama model {model} does not support tools", 400)
                        if r.status_code == 400 and NO_PICTURE in text.lower():
                            said = error_from_status(400, text, None).message.removeprefix("HTTP 400: ")
                            raise ProviderError("bad_request", PICTURE_REFUSED.format(said=said[:200], model=model), 400,
                                                complete=True)
                        raise error_from_status(r.status_code, text, None)
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if obj.get("error"):
                            raise ProviderError("unavailable", f"Ollama: {obj['error']}")
                        msg = obj.get("message") or {}
                        if msg.get("thinking"):
                            yield ThinkingDelta(msg["thinking"])
                        if msg.get("content"):
                            yield TextDelta(msg["content"])
                        calls = msg.get("tool_calls")
                        for tc in calls if isinstance(calls, list) else []:
                            fn = (tc.get("function") if isinstance(tc, dict) else None) or {}
                            cid = safe_call_id(tc.get("id") if isinstance(tc, dict) else None, taken)
                            taken.add(cid)
                            if not fn.get("name"):
                                yield ToolCall(cid, "", {}, error="tool call without a name")
                                continue
                            args, err = parse_arguments(fn.get("arguments"))
                            yield ToolCall(cid, fn["name"], args, error=err)
                        if obj.get("done"):
                            usage = Usage(int(obj.get("prompt_eval_count") or 0),
                                          int(obj.get("eval_count") or 0))
                            reason = obj.get("done_reason") or "end"
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", "Ollama timed out") from e
        except httpx.HTTPError as e:
            raise ProviderError(
                "unavailable", f"Ollama not reachable at {host()} ({type(e).__name__})") from e
        yield usage
        yield Stop(reason)


def _chars(body: dict) -> int:
    """The characters of a request as the model gets it: every message, every tool definition."""
    total = len(json.dumps(body["tools"])) if body.get("tools") else 0
    for m in body["messages"]:
        total += len(m.get("content") or "") + IMAGE_CHARS * len(m.get("images") or [])
        if m.get("tool_calls"):
            total += len(json.dumps(m["tool_calls"]))
    return total


def make() -> OllamaProvider:
    return OllamaProvider()
