"""Adapter for Gemini models through the google-genai SDK."""
from __future__ import annotations

import base64

import httpx
from google import genai
from google.genai import errors

from .. import config
from .base import (ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta, ToolCall,
                   ToolSpec, Usage, WireMessage, call_id, error_from_status, render_file_block,
                   rounds)

KEY_ENV = "GEMINI_API_KEY"
OPTIONS = ["temperature", "top_p", "max_tokens", "thinking", "thinking_budget"]


def _parts(m: WireMessage) -> list[dict]:
    parts: list[dict] = []
    for b in m.blocks:
        if b.type == "text" and b.text:
            parts.append({"text": b.text})
        elif b.type == "file":
            parts.append({"text": render_file_block(b)})
        elif b.type == "image" and m.role == "user" and b.upload_id in m.images:
            parts.append({"inline_data": {"mime_type": b.mime, "data": m.images[b.upload_id]}})
    return parts


def _model_turns(m: WireMessage) -> list[dict]:
    """An assistant message round by round. Gemini pairs calls and responses by order and
    name, so ids are not sent. A thought signature goes back only on the part it arrived
    on, unchanged; it is never invented, copied, or dropped."""
    out: list[dict] = []
    for r in rounds(m):
        parts: list[dict] = []
        names: dict[str, str] = {}
        for b in r.blocks:
            if b.type == "text" and b.text:
                parts.append({"text": b.text})
            elif b.type == "tool_call":
                names[b.id] = b.name
                part: dict = {"function_call": {"name": b.name, "args": b.arguments}}
                if b.opaque and b.opaque.get("provider") == "google" \
                        and b.opaque.get("thought_signature"):
                    part["thought_signature"] = base64.b64decode(b.opaque["thought_signature"])
                parts.append(part)
        if parts:
            out.append({"role": "model", "parts": parts})
        if r.results:
            out.append({"role": "user", "parts": [
                {"function_response": {
                    "name": names[res.tool_call_id],
                    "response": {"error" if res.is_error else "output": res.content}}}
                for res in r.results]})
    return out


class GoogleProvider:
    name = "google"
    supports_tools = True

    def __init__(self, client=None):
        self._client = client

    def configured(self) -> bool:
        return bool(config.env(KEY_ENV))

    def _get_client(self):
        if self._client is not None:
            return self._client
        key = config.env(KEY_ENV)
        if not key:
            raise ProviderError("auth", f"{KEY_ENV} is not set")
        return genai.Client(api_key=key)

    async def models(self) -> list[ModelInfo]:
        try:
            pager = await self._get_client().aio.models.list()
            out = []
            async for m in pager:
                if "generateContent" not in (getattr(m, "supported_actions", None) or []):
                    continue
                mid = (m.name or "").removeprefix("models/")
                out.append(ModelInfo(id=mid, label=getattr(m, "display_name", None) or mid,
                                     supports_images=True, options=list(OPTIONS),
                                     context_window=getattr(m, "input_token_limit", None)))
            return out
        except (errors.APIError, ProviderError, httpx.HTTPError):
            return []

    def _config(self, system: str, options: Options,
                tools: list[ToolSpec] | None = None) -> dict:
        o = options.only(OPTIONS)
        cfg: dict = {}
        if system:
            cfg["system_instruction"] = system
        if o.temperature is not None:
            cfg["temperature"] = o.temperature
        if o.top_p is not None:
            cfg["top_p"] = o.top_p
        if o.max_tokens is not None:
            cfg["max_output_tokens"] = o.max_tokens
        if o.thinking is False:
            cfg["thinking_config"] = {"thinking_budget": 0}
        else:
            cfg["thinking_config"] = {"include_thoughts": True}
            if o.thinking_budget is not None:
                cfg["thinking_config"]["thinking_budget"] = o.thinking_budget
        if tools:
            cfg["tools"] = [{"function_declarations": [
                {"name": t.name, "description": t.description,
                 "parameters_json_schema": t.parameters} for t in tools]}]
            # HunterAI runs tools itself, behind its permission engine.
            cfg["automatic_function_calling"] = {"disable": True}
        return cfg

    async def stream(self, model: str, system: str, messages: list[WireMessage],
                     options: Options, tools: list[ToolSpec] | None = None):
        client = self._get_client()
        contents: list[dict] = []
        for m in messages:
            if m.role == "assistant" and any(b.type == "tool_call" for b in m.blocks):
                contents += _model_turns(m)
            elif (p := _parts(m)):
                contents.append({"role": "model" if m.role == "assistant" else "user", "parts": p})
        usage, reason = Usage(0, 0), "end"
        try:
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=contents, config=self._config(system, options, tools))
            async for chunk in stream:
                um = getattr(chunk, "usage_metadata", None)
                if um:
                    out = (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
                    usage = Usage(int(um.prompt_token_count or 0), int(out))
                for cand in getattr(chunk, "candidates", None) or []:
                    fr = getattr(cand, "finish_reason", None)
                    if fr is not None:
                        reason = getattr(fr, "name", None) or str(fr)
                    content = getattr(cand, "content", None)
                    for part in (getattr(content, "parts", None) or []):
                        fc = getattr(part, "function_call", None)
                        if fc is not None:
                            sig = getattr(part, "thought_signature", None)
                            yield ToolCall(call_id(), fc.name or "", dict(fc.args or {}), opaque={
                                "provider": "google", "model": model,
                                "thought_signature": base64.b64encode(sig).decode()} if sig else None)
                            continue
                        if not part.text:
                            continue
                        yield ThinkingDelta(part.text) if part.thought else TextDelta(part.text)
        except errors.APIError as e:
            raise error_from_status(int(e.code or 500), str(e.message or ""), None) from e
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", "Gemini request timed out") from e
        except httpx.HTTPError as e:
            raise ProviderError("unavailable", "Gemini API unreachable") from e
        yield usage
        yield Stop(reason)


def make() -> GoogleProvider:
    return GoogleProvider()
