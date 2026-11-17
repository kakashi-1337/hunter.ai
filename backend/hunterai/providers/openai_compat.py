"""Adapter for any OpenAI-compatible /chat/completions endpoint.

Registered three times: abliteration.ai (the default provider), OpenAI, and xAI.
"""
from __future__ import annotations

import base64
import json

import httpx

from .. import config
from .base import (IMAGE_OMITTED, ModelInfo, Options, ProviderError, Stop, TextDelta,
                   ThinkingDelta, ToolCall, ToolSpec, Usage, WireMessage, error_from_status,
                   flatten_text, parse_arguments, render_file_block, rounds, safe_call_id, through_proxy, way_out)

BASIC = ["temperature", "top_p", "max_tokens"]
TIMEOUT = httpx.Timeout(120.0, connect=30.0)   # a busy provider can take more than ten seconds to accept a connection

_ABLIT = ["temperature", "top_p", "max_tokens", "effort"]
ABLIT_MODELS = [
    ModelInfo(id="abliterated-model", label="Abliterated", supports_images=True,
              options=_ABLIT + ["thinking"], context_window=262144),
    ModelInfo(id="abliterated-model-large-flash", label="Abliterated Large Flash",
              supports_images=True, options=_ABLIT + ["thinking"], context_window=1048576),
    # v2 cannot disable reasoning, so it gets no thinking toggle.
    ModelInfo(id="abliterated-model-large-v2", label="Abliterated Large v2",
              supports_images=False, options=_ABLIT, context_window=1000000),
]


# How a provider names an error it sends inside a 200 stream, in the order they are tried.
_STREAM_ERROR_KINDS = [
    ("auth", ("invalid_api_key", "incorrect_api_key", "authentication", "unauthorized")),
    ("credits", ("insufficient_quota", "insufficient_credits", "billing", "payment_required")),
    ("rate_limit", ("rate_limit", "too_many_requests")),
    ("policy", ("content_filter", "content_policy", "moderation", "permission_denied")),
    ("timeout", ("timeout", "timed_out")),
    ("bad_request", ("invalid_request", "bad_request", "context_length", "model_not_found")),
]


class OpenAICompatProvider:
    supports_tools = True

    def __init__(self, name: str, base_url: str, key_env: str,
                 default_models: list[ModelInfo] | None = None,
                 unknown_options: list[str] | None = None,
                 max_tokens_param: str = "max_tokens"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self.default_models = default_models or []
        self.unknown_options = unknown_options or BASIC
        self.max_tokens_param = max_tokens_param

    def configured(self) -> bool:
        return bool(config.env(self.key_env))

    def _info(self, model_id: str) -> ModelInfo:
        for m in self.default_models:
            if m.id == model_id:
                return m
        return ModelInfo(id=model_id, label=model_id, options=list(self.unknown_options))

    def _headers(self) -> dict:
        key = config.env(self.key_env)
        if not key:
            raise ProviderError("auth", f"{self.key_env} is not set")
        return {"Authorization": f"Bearer {key}"}

    def _scrub(self, text: str) -> str:
        key = config.env(self.key_env)
        return text.replace(key, "[key]") if key else text

    def _stream_error(self, err) -> ProviderError:
        """An {"error": ...} chunk inside a 200 stream, as the error its HTTP status would be."""
        fields = err if isinstance(err, dict) else {"message": err}
        message = self._scrub(str(fields.get("message") or err))[:300]
        for key in ("code", "status"):
            code = str(fields.get(key) or "")
            if code.isdigit() and 400 <= int(code) <= 599:
                return error_from_status(int(code), json.dumps({"error": {"message": message}}),
                                         None)
        for text in (str(fields.get("code") or ""), str(fields.get("type") or ""),
                     str(fields.get("message") or err)):
            text = text.lower().replace(" ", "_")
            for kind, needles in _STREAM_ERROR_KINDS:
                if any(n in text for n in needles):
                    return ProviderError(kind, message)
        return ProviderError("unavailable", message)

    async def models(self) -> list[ModelInfo]:
        try:
            async with httpx.AsyncClient(timeout=10.0, proxy=await way_out()) as c:
                r = await c.get(f"{self.base_url}/models", headers=self._headers())
                r.raise_for_status()
                ids = [m["id"] for m in r.json().get("data", []) if m.get("id")]
        except (httpx.HTTPError, ProviderError, ValueError, KeyError, TypeError):
            return list(self.default_models)
        return [self._info(i) for i in ids] or list(self.default_models)

    def _message(self, m: WireMessage, images_ok: bool) -> dict:
        has_image = any(b.type == "image" for b in m.blocks)
        if m.role != "user" or not (images_ok and has_image):
            return {"role": m.role, "content": flatten_text(m)}
        parts: list[dict] = []
        for b in m.blocks:
            if b.type == "text" and b.text:
                parts.append({"type": "text", "text": b.text})
            elif b.type == "file":
                parts.append({"type": "text", "text": render_file_block(b)})
            elif b.type == "image":
                data = m.images.get(b.upload_id)
                if data is None:
                    parts.append({"type": "text", "text": IMAGE_OMITTED})
                else:
                    url = f"data:{b.mime};base64,{base64.b64encode(data).decode()}"
                    parts.append({"type": "image_url", "image_url": {"url": url}})
        return {"role": "user", "content": parts}

    def _assistant(self, m: WireMessage) -> list[dict]:
        """An assistant message round by round: text and calls, then one tool message per
        result. Thinking is not sent back."""
        out: list[dict] = []
        for r in rounds(m):
            text = "\n\n".join(b.text for b in r.blocks if b.type == "text" and b.text)
            calls = [{"id": b.id, "type": "function",
                      "function": {"name": b.name,
                                   "arguments": json.dumps(b.arguments, ensure_ascii=False)}}
                     for b in r.blocks if b.type == "tool_call"]
            entry: dict = {"role": "assistant"}
            if text or not calls:
                entry["content"] = text
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            out += [{"role": "tool", "tool_call_id": res.tool_call_id, "content": res.content}
                    for res in r.results]
        return out

    def _payload(self, model: str, system: str, messages: list[WireMessage],
                 options: Options, tools: list[ToolSpec] | None = None) -> dict:
        info = self._info(model)
        o = options.only(info.options)
        body: dict = {"model": model, "stream": True,
                      "stream_options": {"include_usage": True}, "messages": []}
        if system:
            body["messages"].append({"role": "system", "content": system})
        for m in messages:
            if m.role == "assistant" and any(b.type == "tool_call" for b in m.blocks):
                body["messages"] += self._assistant(m)
            else:
                body["messages"].append(self._message(m, info.supports_images))
        if tools:
            body["tools"] = [{"type": "function",
                              "function": {"name": t.name, "description": t.description,
                                           "parameters": t.parameters}} for t in tools]
        if o.temperature is not None:
            body["temperature"] = o.temperature
        if o.top_p is not None:
            body["top_p"] = o.top_p
        if o.max_tokens is not None:
            body[self.max_tokens_param] = o.max_tokens
        if o.json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "answer", "schema": o.json_schema, "strict": True}}
        if o.thinking is False:
            body["reasoning_effort"] = "none"
        elif o.effort is not None:
            body["reasoning_effort"] = o.effort
        return body

    async def stream(self, model: str, system: str, messages: list[WireMessage],
                     options: Options, tools: list[ToolSpec] | None = None):
        headers = self._headers()
        payload = self._payload(model, system, messages, options, tools)
        usage, reason = Usage(0, 0), "end"
        pending: dict[int, dict] = {}   # tool-call fragments by index
        try:
            via = await way_out()
            async with httpx.AsyncClient(timeout=TIMEOUT, proxy=via) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions",
                                         headers=headers, json=payload) as r:
                    if via:                  # an answer the proxy made itself is not the provider's
                        from .. import hop
                        if hop.answered_itself(r):
                            raise ProviderError("unavailable", f"{self.name} was not reached: the proxy for the model "
                                                               f"providers did not carry the request ({hop.answered_itself(r)})")
                    if r.status_code >= 400:
                        body = (await r.aread()).decode("utf-8", "replace")
                        raise error_from_status(r.status_code, self._scrub(body),
                                                r.headers.get("retry-after"))
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            continue
                        if isinstance(chunk, dict) and chunk.get("error"):
                            raise self._stream_error(chunk["error"])
                        u = chunk.get("usage")
                        if u:
                            usage = Usage(int(u.get("prompt_tokens") or 0),
                                          int(u.get("completion_tokens") or 0))
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            thought = delta.get("reasoning_content") or delta.get("reasoning")
                            if isinstance(thought, str) and thought:
                                yield ThinkingDelta(thought)
                            if delta.get("content"):
                                yield TextDelta(delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                if not isinstance(tc, dict):
                                    continue
                                slot = pending.setdefault(
                                    int(tc.get("index") or 0), {"id": None, "name": "", "args": ""})
                                fn = tc.get("function") or {}
                                slot["id"] = slot["id"] or tc.get("id")
                                slot["name"] = slot["name"] or (fn.get("name") or "")
                                if isinstance(fn.get("arguments"), str):
                                    slot["args"] += fn["arguments"]
                                elif isinstance(fn.get("arguments"), dict):
                                    slot["args"] = json.dumps(fn["arguments"])
                            if choice.get("finish_reason"):
                                reason = choice["finish_reason"]
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", f"{self.name} timed out") from e
        except httpx.HTTPError as e:
            raise ProviderError("unavailable",
                                f"{self.name} unreachable: {type(e).__name__}{through_proxy(e)}") from e
        taken: set[str] = set()
        for index in sorted(pending):
            slot = pending[index]
            call_id = safe_call_id(slot["id"], taken)
            taken.add(call_id)
            if not slot["name"]:
                yield ToolCall(call_id, "", {}, error="tool call without a name")
                continue
            args, err = parse_arguments(slot["args"])
            yield ToolCall(call_id, slot["name"], args, error=err)
        yield usage
        yield Stop(reason)


def abliteration() -> OpenAICompatProvider:
    base = config.env("HUNTERAI_ABLIT_BASE_URL") or "https://api.abliteration.ai/v1"
    return OpenAICompatProvider("abliteration", base, "ABLIT_API_KEY", ABLIT_MODELS)


def openai() -> OpenAICompatProvider:
    return OpenAICompatProvider("openai", "https://api.openai.com/v1", "OPENAI_API_KEY",
                                unknown_options=BASIC + ["effort"],
                                max_tokens_param="max_completion_tokens")


def xai() -> OpenAICompatProvider:
    return OpenAICompatProvider("xai", "https://api.x.ai/v1", "XAI_API_KEY",
                                unknown_options=BASIC + ["effort"])
