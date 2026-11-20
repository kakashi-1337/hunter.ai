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


def slow_connections(monkeypatch, delay=0.05, fetch=0.0):
    """Every database connection opened from now on takes `delay` for each of its settings, as on a
    slow disk or a busy machine, and `fetch` between a statement and the reading of its rows.
    Returns them in the order they were opened; one is closed when its worker thread has ended
    (`still_open` says which are not)."""
    from hunterai import db
    opened = []
    real = db.aiosqlite.connect

    def connect(*args, **kwargs):
        c = real(*args, **kwargs)
        opened.append(c)
        execute = c.execute

        async def later(sql, *a, **kw):
            if str(sql).startswith("pragma"):
                await asyncio.sleep(delay)
            return await execute(sql, *a, **kw)
        c.execute = later
        return c
    # The reading is slowed on the class, not on each cursor: a function kept on a cursor would keep the
    # cursor alive after its statement, and a statement that is still open is what the tests look for.
    for name in ("fetchone", "fetchall") if fetch else ():
        read = getattr(db.aiosqlite.Cursor, name)

        async def slowly(self, *a, _read=read, **kw):
            await asyncio.sleep(fetch)
            return await _read(self, *a, **kw)
        monkeypatch.setattr(db.aiosqlite.Cursor, name, slowly)
    monkeypatch.setattr(db.aiosqlite, "connect", connect)
    return opened


def still_open(connections) -> list:
    return [c for c in connections if c._thread.is_alive()]


async def runs_have_ended(timeout=5.0) -> None:
    """Wait until every run this test started is done with its last step, closing its database connection
    included. One that would never end by itself (it waits for an answer nobody gives) is stopped."""
    from hunterai import engine
    here = asyncio.get_running_loop()
    alive = [t for t in engine._tasks.values() if not t.done() and t.get_loop() is here]
    if alive:
        _, still = await asyncio.wait(alive, timeout=timeout)
        if still:
            await engine.shutdown()


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
