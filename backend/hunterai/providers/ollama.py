"""Adapter for a local Ollama server (native /api/chat, NDJSON streaming)."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx

from .. import config
from .base import (ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta, ToolCall,
                   ToolSpec, Usage, WireMessage, error_from_status, flatten_text,
                   parse_arguments, rounds, safe_call_id)

OPTIONS = ["temperature", "top_p", "max_tokens", "context_window", "thinking"]
TIMEOUT = httpx.Timeout(300.0, connect=5.0)


DEFAULT_NUM_CTX = 8192


def host() -> str:
    h = (config.env("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    return h.replace("0.0.0.0", "127.0.0.1")


def _is_embedder(name: str) -> bool:
    n = name.lower()
    return "embed" in n or n.startswith("bge-")


class OllamaProvider:
    supports_tools = True

    name = "ollama"

    def __init__(self):
        self._vision: dict[str, bool] = {}

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
                vision = "vision" in (r.json().get("capabilities") or [])
        except (httpx.HTTPError, ValueError, AttributeError):
            return False  # unknown: send no images, and ask again next time
        self._vision[model] = vision
        return vision

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
        return [ModelInfo(id=n, label=n, options=list(OPTIONS), supports_images=v)
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
        # Always send num_ctx: the server default can be huge and overload the host.
        opts = {k: v for k, v in (("num_ctx", o.context_window or DEFAULT_NUM_CTX),
                                  ("num_predict", o.max_tokens),
                                  ("temperature", o.temperature), ("top_p", o.top_p))
                if v is not None}
        body: dict = {"model": model, "stream": True, "options": opts, "messages": []}
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
        if o.thinking is not None:
            body["think"] = o.thinking
        if tools:
            body["tools"] = [{"type": "function",
                              "function": {"name": t.name, "description": t.description,
                                           "parameters": t.parameters}} for t in tools]
        taken: set[str] = set()
        usage, reason = Usage(0, 0), "end"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                async with client.stream("POST", f"{host()}/api/chat", json=body) as r:
                    if r.status_code == 404:
                        raise ProviderError(
                            "bad_request",
                            f"Ollama does not have model {model}. Run: ollama pull {model}", 404)
                    if r.status_code >= 400:
                        text = (await r.aread()).decode("utf-8", "replace")
                        if r.status_code == 400 and "does not support tools" in text:
                            raise ProviderError(
                                "bad_request", f"Ollama model {model} does not support tools", 400)
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


def make() -> OllamaProvider:
    return OllamaProvider()
