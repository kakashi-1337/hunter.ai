"""Adapter for Gemini models through the google-genai SDK."""
from __future__ import annotations

import base64
import re

import httpx
from google import genai
from google.genai import errors, types

from .. import config
from .base import (ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta, ToolCall,
                   ToolSpec, Usage, WireMessage, call_id, error_from_status, render_file_block,
                   rounds, way_out)

KEY_ENV = "GEMINI_API_KEY"
OPTIONS = ["temperature", "top_p", "max_tokens", "thinking", "thinking_budget", "effort"]
_BASE = ["temperature", "top_p", "max_tokens", "thinking"]
# Thinking levels by model, from https://ai.google.dev/gemini-api/docs/thinking (read 2026-11-16): the
# longest matching start of the name decides. A Gemini 3 model that is not listed gets the common three.
_LEVELS = (
    ("gemini-3.1-flash-lite-image", ("minimal", "high")),
    ("gemini-3.5-flash-lite", ("minimal", "low", "medium", "high")),
    ("gemini-3.6-flash", ("minimal", "low", "medium", "high")),
    ("gemini-3.8-flash", ("low", "medium", "high")),
    ("gemini-3.1-pro", ("low", "medium", "high")),
    ("gemini-3-flash", ("minimal", "low", "medium", "high")),
    ("gemini-3-pro", ("low", "high")),
)
_COMMON = ("low", "medium", "high")
_ORDER = ("minimal", "low", "medium", "high")
_BUDGET = {"none": 0, "low": 1024, "medium": 8192}      # Gemini 2.5 and earlier: tokens for an effort
_BUDGET_HIGH = 24576                                    # the most a 2.5 Flash takes; a 2.5 Pro takes it too
_VERSION = re.compile(r"^gemini-(\d+)")


def _thinks_by(model: str) -> tuple[str, tuple[str, ...]]:
    """("level", the levels the model has) for Gemini 3 and later, ("budget", ()) for 2.5 and earlier,
    ("", ()) for a model that is not a Gemini model. An alias with no number (gemini-flash-latest) is
    the current generation."""
    if not model.startswith("gemini-"):
        return "", ()
    m = _VERSION.match(model)
    if m and int(m.group(1)) < 3:
        return "budget", ()
    return "level", next((levels for start, levels in _LEVELS if model.startswith(start)), _COMMON)


def _level(effort: str, levels: tuple[str, ...]) -> str:
    """The model's level for an effort: that level, else the next one up it has, else its highest."""
    wanted = {"none": 0, "low": 1, "medium": 2}.get(effort, 3)
    have = sorted(levels, key=_ORDER.index)
    return next((v for v in have if _ORDER.index(v) >= wanted), have[-1])


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

    def _get_client(self, via: str | None = None):
        """`via`: the local hop to the owner's proxy for the model providers (`base.way_out`), when one is set."""
        if self._client is not None:
            return self._client
        key = config.env(KEY_ENV)
        if not key:
            raise ProviderError("auth", f"{KEY_ENV} is not set")
        if via:
            return genai.Client(api_key=key, http_options=types.HttpOptions(
                client_args={"proxy": via}, async_client_args={"proxy": via}))
        return genai.Client(api_key=key)

    async def models(self) -> list[ModelInfo]:
        try:
            pager = await self._get_client(await way_out()).aio.models.list()
            out = []
            async for m in pager:
                if "generateContent" not in (getattr(m, "supported_actions", None) or []):
                    continue
                mid = (m.name or "").removeprefix("models/")
                kind, levels = _thinks_by(mid)
                efforts = [v for v in (levels or _COMMON) if v != "minimal"] if kind else []    # minimal is "thinking: off"
                out.append(ModelInfo(id=mid, label=getattr(m, "display_name", None) or mid, supports_images=True,
                                     options=_BASE + ([] if kind == "level" else ["thinking_budget"]) + (["effort"] if kind else []),
                                     context_window=getattr(m, "input_token_limit", None), efforts=efforts))
            return out
        except (errors.APIError, ProviderError, httpx.HTTPError):
            return []

    def _config(self, system: str, options: Options,
                tools: list[ToolSpec] | None = None, model: str = "") -> dict:
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
        kind, levels = _thinks_by(model)
        if kind == "level":
            # A level and a budget in one request is an error, and the Pro models cannot be switched off:
            # off is the lowest level the model has.
            if o.thinking is False:
                cfg["thinking_config"] = {"thinking_level": _level("none", levels)}
            else:
                cfg["thinking_config"] = {"include_thoughts": True}
                if o.effort is not None:
                    cfg["thinking_config"]["thinking_level"] = _level(o.effort, levels)
        elif o.thinking is False:
            cfg["thinking_config"] = {"thinking_budget": 0}
        else:
            cfg["thinking_config"] = {"include_thoughts": True}
            if o.thinking_budget is not None:
                cfg["thinking_config"]["thinking_budget"] = o.thinking_budget
            elif kind == "budget" and o.effort is not None:
                cfg["thinking_config"]["thinking_budget"] = _BUDGET.get(o.effort, _BUDGET_HIGH)
        if tools:
            cfg["tools"] = [{"function_declarations": [
                {"name": t.name, "description": t.description,
                 "parameters_json_schema": t.parameters} for t in tools]}]
            # HunterAI runs tools itself, behind its permission engine.
            cfg["automatic_function_calling"] = {"disable": True}
        return cfg

    async def stream(self, model: str, system: str, messages: list[WireMessage],
                     options: Options, tools: list[ToolSpec] | None = None):
        client = self._get_client(await way_out())
        contents: list[dict] = []
        for m in messages:
            if m.role == "assistant" and any(b.type == "tool_call" for b in m.blocks):
                contents += _model_turns(m)
            elif (p := _parts(m)):
                contents.append({"role": "model" if m.role == "assistant" else "user", "parts": p})
        usage, reason = Usage(0, 0), "end"
        try:
            stream = await client.aio.models.generate_content_stream(
                model=model, contents=contents, config=self._config(system, options, tools, model))
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
