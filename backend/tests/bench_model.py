"""A stand-in for a model in the tests of the bench: it replays one list of events per model call."""
from __future__ import annotations

from hunterai.providers.base import (
    ModelInfo,
    ProviderError,
    Stop,
    TextDelta,
    ToolCall,
    Usage,
)


def says(text: str) -> list:
    """A model call that answers."""
    return [TextDelta(text), Usage(10, 5), Stop("end")]


def calls(name: str, **arguments) -> list:
    """A model call that calls one tool. Two of them added up are one model call with two tool calls."""
    return [ToolCall("call_1", name, arguments), Usage(10, 5), Stop("tool_calls")]


def fails(kind: str, message: str) -> list:
    """A model call the provider fails."""
    return [ProviderError(kind, message)]


class Model:
    """`turns`: one list of events per model call, the last one repeated for every further call. An exception
    in a list is raised at that place. `seen` holds what each call was sent."""
    name = "scripted"
    supports_tools = True

    def __init__(self, *turns):
        self.turns = [list(t) for t in turns] or [says("ok")]
        self.seen: list[dict] = []

    def configured(self) -> bool:
        return True

    async def models(self) -> list[ModelInfo]:
        return [ModelInfo(id="m", label="m")]

    def _next(self, model, system, messages, options, tools):
        self.seen.append({"model": model, "system": system, "messages": messages, "options": options, "tools": tools})
        return self.turns[min(len(self.seen), len(self.turns)) - 1]

    async def stream(self, model, system, messages, options, tools=None):
        for ev in self._next(model, system, messages, options, tools):
            if isinstance(ev, BaseException):
                raise ev
            yield ev


class Wordy(Model):
    """A model that cannot be given tools: its stream takes none."""
    supports_tools = False

    async def stream(self, model, system, messages, options):
        for ev in self._next(model, system, messages, options, None):
            if isinstance(ev, BaseException):
                raise ev
            yield ev
