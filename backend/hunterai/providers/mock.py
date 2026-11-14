"""Deterministic provider for tests and offline UI work. Enabled with HUNTERAI_MOCK=1."""
from __future__ import annotations

import asyncio

from .base import (ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta, ToolCall,
                   ToolSpec, Usage, WireMessage, flatten_text, parse_arguments)


class MockProvider:
    name = "mock"
    supports_tools = True

    def configured(self) -> bool:
        return True

    async def models(self) -> list[ModelInfo]:
        return [ModelInfo(id="mock-1", label="Mock", supports_images=True,
                          options=["temperature", "max_tokens", "effort", "thinking"])]

    async def stream(self, model: str, system: str, messages: list[WireMessage],
                     options: Options, tools: list[ToolSpec] | None = None):
        text = next((flatten_text(m, images_ok=True) for m in reversed(messages)
                     if m.role == "user"), "")
        script, _, rest = text.partition(" ")
        if tools and script in ("call", "call-bad", "loop"):
            tool, _, raw = rest.partition(" ")
            last = messages[-1] if messages and messages[-1].role == "assistant" else None
            done = [b for b in last.blocks if b.type == "tool_call"] if last else []
            if script != "loop" and last and last.blocks and last.blocks[-1].type == "tool_result":
                yield TextDelta("tool said: " + last.blocks[-1].content[:200])
                yield Usage(3, 5)
                yield Stop("end")
                return
            if script == "call-bad":
                yield ToolCall("call_1", tool, {}, error="arguments are not valid JSON: mock")
            else:
                args, err = parse_arguments(raw.strip())
                n = len(done) + 1 if script == "loop" else 1
                yield ToolCall(f"call_{n}", tool, args, error=err)
            yield Usage(3, 5)
            yield Stop("tool_calls")
            return
        if text.startswith("fail:"):
            raise ProviderError(text.split(":", 1)[1].strip(), "mock failure")
        if text == "partial-fail":
            yield TextDelta("half")
            raise ProviderError("unavailable", "mock dropped the stream")
        if text == "slow":
            for i in range(5):
                await asyncio.sleep(0.05)
                yield TextDelta(f"chunk{i} ")
            yield Usage(3, 5)
            yield Stop("end")
            return
        yield ThinkingDelta("thinking")
        yield TextDelta("echo: " + text)
        yield Usage(3, 5)
        yield Stop("end")
