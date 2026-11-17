"""Adapter for the Claude API, through the official anthropic SDK."""
from __future__ import annotations

import base64

import anthropic

from .. import config
from .base import (ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                   ThinkingSeal, ToolCall, ToolSpec, Usage, WireMessage, error_from_status,
                   render_file_block, rounds, safe_call_id)

KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MAX_TOKENS = 64000
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models whose safety classifiers can decline a request; these get server-side fallbacks.
REFUSAL_FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5",
                           "claude-opus-5", "claude-sonnet-5-5"}
# Pre-4.6 models: no adaptive thinking and no effort parameter.
_LEGACY_MARKERS = ("claude-3", "-4-0", "-4-1", "-4-5", "claude-opus-4-2", "claude-sonnet-4-2")
STATIC_MODELS = [
    ("claude-opus-5-5", "Claude Opus 5.5"), ("claude-fable-5-2", "Claude Fable 5.2"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"), ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-opus-5", "Claude Opus 5"), ("claude-sonnet-5", "Claude Sonnet 5"),
]


def is_legacy(model: str) -> bool:
    return any(m in model for m in _LEGACY_MARKERS)


def _info(model_id: str, label: str, context: int | None) -> ModelInfo:
    options = ["max_tokens"] if is_legacy(model_id) else ["max_tokens", "effort"]
    return ModelInfo(id=model_id, label=label, supports_images=True, options=options,
                     context_window=context)


def _content(m: WireMessage) -> list[dict]:
    images, rest = [], []
    for b in m.blocks:
        if b.type == "image" and m.role == "user" and b.upload_id in m.images:
            data = base64.standard_b64encode(m.images[b.upload_id]).decode()
            images.append({"type": "image", "source": {"type": "base64", "media_type": b.mime,
                                                       "data": data}})
        elif b.type == "text" and b.text:
            rest.append({"type": "text", "text": b.text})
        elif b.type == "file":
            rest.append({"type": "text", "text": render_file_block(b)})
    return images + rest  # images first reads best for the model


def _assistant(m: WireMessage, live: bool) -> list[dict]:
    """An assistant message round by round: [thinking, text, tool_use] in block order, then
    one user message with every tool_result of the round.

    Thinking blocks go back byte for byte, and only for the run in progress (`live`). Those
    of earlier messages are left out: removing a leading run of thinking blocks is the one
    removal the API allows, and it keeps the request valid when the mode, target, or tool
    list changed between runs."""
    out: list[dict] = []
    for r in rounds(m):
        content: list[dict] = []
        for b in r.blocks:
            if b.type == "thinking":
                if live and b.opaque and b.opaque.get("provider") == "anthropic":
                    content.append(b.opaque["block"])
            elif b.type == "text" and b.text:
                content.append({"type": "text", "text": b.text})
            elif b.type == "tool_call":
                content.append({"type": "tool_use", "id": b.id, "name": b.name,
                                "input": b.arguments})
        if content:
            out.append({"role": "assistant", "content": content})
        if r.results:
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": res.tool_call_id, "content": res.content,
                 "is_error": res.is_error} for res in r.results]})
    return out


class AnthropicProvider:
    name = "anthropic"
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
        return anthropic.AsyncAnthropic(api_key=key)

    async def models(self) -> list[ModelInfo]:
        try:
            client = self._get_client()
            out = []
            async for m in client.models.list():
                if is_legacy(m.id):
                    continue        # the older models have a fifth of the context window; a chat that uses one keeps it
                out.append(_info(m.id, getattr(m, "display_name", None) or m.id,
                                 getattr(m, "max_input_tokens", None)))
            if out:
                return out
        except (anthropic.APIError, ProviderError):
            pass
        return [_info(i, label, 1000000) for i, label in STATIC_MODELS]

    def _request(self, model: str, system: str, messages: list[WireMessage],
                 options: Options, tools: list[ToolSpec] | None = None) -> tuple[bool, dict]:
        kw: dict = {"model": model, "max_tokens": options.max_tokens or DEFAULT_MAX_TOKENS,
                    "messages": []}
        if system:
            kw["system"] = system
        entries: list[dict] = []
        for i, m in enumerate(messages):
            if m.role == "assistant":
                entries += _assistant(m, live=i == len(messages) - 1)
            else:
                content = _content(m)
                if content:
                    entries.append({"role": m.role, "content": content})
        results_last = False   # the previous entry is a round's tool results
        for e in entries:
            is_results = e["role"] == "user" and e["content"][0].get("type") == "tool_result"
            if results_last and e["role"] == "user":
                # Tool results and what the user said next share one message, results first.
                kw["messages"][-1]["content"] += e["content"]
            else:
                kw["messages"].append({"role": e["role"], "content": list(e["content"])})
                results_last = is_results
        if tools:
            # Client tools on a streamed request: stream large inputs as they are generated.
            kw["tools"] = [{"name": t.name, "description": t.description,
                            "input_schema": t.parameters, "eager_input_streaming": True}
                           for t in sorted(tools, key=lambda t: t.name)]
        if not is_legacy(model):
            # Thinking text is hidden by default on current models; ask for the summary.
            kw["thinking"] = {"type": "adaptive", "display": "summarized"}
            if options.effort and options.effort != "none":
                kw["output_config"] = {"effort": options.effort}
        use_beta = model in REFUSAL_FALLBACK_MODELS
        if use_beta:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return use_beta, kw

    async def stream(self, model: str, system: str, messages: list[WireMessage],
                     options: Options, tools: list[ToolSpec] | None = None):
        client = self._get_client()
        use_beta, kw = self._request(model, system, messages, options, tools)
        api = client.beta.messages if use_beta else client.messages
        emitted = False
        started: list[tuple[str | None, str]] = []   # tool_use blocks that began
        finished: list = []                          # tool_use blocks that completed
        final = None
        try:
            async with api.stream(**kw) as s:
                async for event in s:
                    cb = getattr(event, "content_block", None)
                    if event.type == "content_block_start":
                        if getattr(cb, "type", None) == "tool_use":
                            started.append((getattr(cb, "id", None), getattr(cb, "name", "")))
                        continue
                    if event.type == "content_block_stop":
                        kind = getattr(cb, "type", None)
                        if kind == "thinking":
                            yield ThinkingSeal({"provider": "anthropic", "model": model, "block": {
                                "type": "thinking", "thinking": cb.thinking,
                                "signature": cb.signature}})
                        elif kind == "redacted_thinking":
                            yield ThinkingSeal({"provider": "anthropic", "model": model, "block": {
                                "type": "redacted_thinking", "data": cb.data}})
                        elif kind == "tool_use":
                            finished.append(cb)
                        continue
                    if event.type != "content_block_delta":
                        continue
                    d = event.delta
                    if d.type == "text_delta" and d.text:
                        emitted = True
                        yield TextDelta(d.text)
                    elif d.type == "thinking_delta" and d.thinking:
                        yield ThinkingDelta(d.thinking)
                final = await s.get_final_message()
        except ValueError as e:
            # Tool input the SDK could not parse. The model can send the call again.
            if not started:
                raise ProviderError(
                    "bad_request",
                    "Claude sent a response that could not be parsed; retry the message") from e
        except anthropic.APITimeoutError as e:
            raise ProviderError("timeout", "Anthropic request timed out") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError("unavailable", "Anthropic API unreachable") from e
        except anthropic.APIStatusError as e:
            raise error_from_status(e.status_code, str(e.message),
                                    e.response.headers.get("retry-after")) from e
        taken: set[str] = set()

        def cid(raw) -> str:
            value = safe_call_id(raw, taken)
            taken.add(value)
            return value

        if final is None:
            for raw_id, name in started:
                yield ToolCall(cid(raw_id), name, {},
                               error="tool input was not valid JSON; send the call again")
            yield Usage(0, 0)
            yield Stop("tool_use")
            return
        reason = final.stop_reason or "end"
        if reason == "refusal" and not emitted:
            raise ProviderError("policy", "The model declined this request (refusal).")
        if reason != "refusal":
            for cb in finished:
                if reason == "max_tokens":
                    yield ToolCall(cid(cb.id), cb.name, {}, error=(
                        "the call was cut off at max_tokens; send it again with shorter arguments"))
                elif not isinstance(cb.input, dict):
                    yield ToolCall(cid(cb.id), cb.name, {}, error="arguments must be a JSON object")
                else:
                    yield ToolCall(cid(cb.id), cb.name, cb.input)
        u = final.usage
        yield Usage(int(u.input_tokens or 0), int(u.output_tokens or 0))
        yield Stop(reason)


def make() -> AnthropicProvider:
    return AnthropicProvider()
