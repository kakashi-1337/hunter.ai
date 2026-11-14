"""Shared helpers for the tool tests."""
from __future__ import annotations

from hunterai.providers.base import (ModelInfo, Options, Stop, TextDelta, ToolSpec, Usage,
                                     WireMessage)
from hunterai.providers.mock import MockProvider
from hunterai.types import TextBlock, ToolCallBlock, ToolResultBlock

SPEC = ToolSpec("execute", "Run a command",
                {"type": "object", "properties": {"command": {"type": "string"}},
                 "required": ["command"]})


def call(id, name, **arguments) -> ToolCallBlock:  # noqa: A002
    return ToolCallBlock(id=id, name=name, arguments=arguments)


def result(id, content, is_error=False) -> ToolResultBlock:  # noqa: A002
    return ToolResultBlock(tool_call_id=id, content=content, is_error=is_error)


class Turns:
    """A tool-capable provider that replays one scripted list of events per model call.

    An exception inside a list is raised at that point; an exception in place of a list is
    raised when that call starts. Usage(3, 5) and Stop("end") are added unless supplied.
    """
    supports_tools = True

    def __init__(self, name, *scripts):
        self.name, self.scripts, self.calls = name, list(scripts), []

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="m", label="m")]

    async def stream(self, model, system, messages, options, tools=None):
        self.calls.append({"model": model, "system": system, "messages": messages,
                           "options": options, "tools": tools})
        script = self.scripts.pop(0) if self.scripts else [TextDelta("done")]
        if isinstance(script, BaseException):
            raise script
        for ev in script:
            if isinstance(ev, BaseException):
                raise ev
            yield ev
        if not any(isinstance(e, Usage) for e in script):
            yield Usage(3, 5)
        if not any(isinstance(e, Stop) for e in script):
            yield Stop("end")


async def run_mock(text: str, tools=None, after=None) -> list:
    wire = [WireMessage("user", [TextBlock(text=text)])]
    if after is not None:
        wire.append(WireMessage("assistant", after))
    return [e async for e in MockProvider().stream("mock-1", "", wire, Options(), tools=tools)]


# ---------------------------------------------------------------- tool runner helpers

import asyncio  # noqa: E402

from hunterai import permissions  # noqa: E402
from hunterai.repo import chats, messages, runs, targets  # noqa: E402
from hunterai.tools import runner  # noqa: E402
from hunterai.tools.base import Prepared, Tool, ToolResult  # noqa: E402


def fake_tool(name, risk="read", *, text="ok", schema=None, grant_key=None, prepared=None,
              raises=None, source="test"):
    """(tool, calls): `calls` records the arguments of every handler call."""
    calls: list[dict] = []

    async def handler(ctx, args):
        calls.append(args)
        if raises is not None:
            raise raises
        return ToolResult(text)

    prepare = None
    if grant_key is not None:
        async def prepare(ctx, args):  # noqa: F811
            return Prepared(grant_target=str(args[grant_key]))
    elif prepared is not None:
        async def prepare(ctx, args):  # noqa: F811
            return prepared
    return Tool(name, "a test tool", schema or {"type": "object"}, risk, handler,
                prepare=prepare, source=source), calls


def collect():
    events: list[dict] = []

    async def publish(event: dict) -> None:
        events.append(event)
    return publish, events


async def answer_next(run_id, decision, note="", timeout=5.0):
    """Answer the first pending permission request (of any run when run_id is None)."""
    for _ in range(int(timeout / 0.01)):
        waiting = permissions.pending(run_id)
        if waiting:
            req = waiting[0]
            assert permissions.answer(req.run_id, req.id, permissions.Answer(decision, note))
            return req
        await asyncio.sleep(0.01)
    raise AssertionError("no permission request was asked")


async def seeded_run(conn, mode="pentesting", provider="abliteration", scope=None, **chat_fields):
    target_id = None
    if scope is not None:
        target_id = (await targets.create(conn, "target", "", scope)).id
    chat = await chats.create(conn, target_id, mode, provider, "m", {})
    if chat_fields:
        chat = await chats.update(conn, chat.id, **chat_fields)
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="hi")])
    reply = await messages.add(conn, chat.id, user.id, "assistant", [], status="streaming")
    run = await runs.create(conn, chat.id, user.id, reply.id)
    return chat, run.id


async def run_call(conn, chat, run_id, tc, offered=None):
    """(outcome, published events, all steps of the run so far)."""
    publish, events = collect()
    if offered is None:
        offered = {t.name for t in await runner.offered_tools(conn, await chats.get(conn, chat.id))}
    out = await runner.call(conn, chat_id=chat.id, run_id=run_id, tc=tc, offered=offered,
                            publish=publish)
    return out, events, (await runs.get(conn, run_id)).steps


async def eventually(pred, timeout=5.0):
    """Wait until pred() is true."""
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not met in time")


def alive(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False
