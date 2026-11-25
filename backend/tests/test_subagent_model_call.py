"""One model call of a subagent: what it appends, what it returns, and what its step records, also when the
call fails or is cancelled after a part of it had come."""
import asyncio

import pytest

from hunterai import subagents
from hunterai.providers import registry
from hunterai.providers.base import (
    Options,
    ProviderError,
    Stop,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
    WireMessage,
)
from hunterai.repo import chats, messages, runs
from hunterai.types import TextBlock
from tests.toolkit import Turns


async def a_step(conn):
    chat = await chats.create(conn, None, "pentesting", "abliteration", "m", {})
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="hi")])
    reply = await messages.add(conn, chat.id, user.id, "assistant", [])
    run = await runs.create(conn, chat.id, user.id, reply.id)
    parent = await runs.add_step(conn, run.id, "tool_call", "delegate", {})
    return run.id, parent.id


async def model_call(conn, run_id, parent, blocks, taken):
    wire = [WireMessage("user", [TextBlock(text="brief")])]
    return await subagents._model_call(conn, run_id, parent, "abliteration", "m", "system", wire, Options(), [], blocks, taken)


async def the_step(conn, run_id):
    return [s for s in (await runs.get(conn, run_id)).steps if s.kind == "model_call"][-1]


async def test_a_subagents_model_call_records_its_blocks_its_calls_and_its_tokens(conn):
    registry.override("abliteration", Turns("abliteration", [ThinkingDelta("hm"), TextDelta("I look."), ToolCall("c1", "read", {"path": "a"}),
                                                             Usage(11, 4), Stop("tool_calls")]))
    run_id, parent = await a_step(conn)
    blocks, taken = [], set()
    calls = await model_call(conn, run_id, parent, blocks, taken)
    assert calls == [ToolCall("c1", "read", {"path": "a"})] and taken == {"c1"}
    assert [b.type for b in blocks] == ["thinking", "text", "tool_call"]
    step = await the_step(conn, run_id)
    assert (step.status, step.input_tokens, step.output_tokens, step.detail) == ("done", 11, 4, {"stop_reason": "tool_calls"})
    assert step.parent_step_id == parent and step.ended_at


async def test_a_subagents_model_call_that_fails_keeps_the_tokens_that_had_come(conn):
    registry.override("abliteration", Turns("abliteration", [TextDelta("par"), Usage(7, 9), Stop("end"),
                                                             ProviderError("unavailable", "gone")]))
    run_id, parent = await a_step(conn)
    blocks = []
    with pytest.raises(ProviderError):
        await model_call(conn, run_id, parent, blocks, set())
    step = await the_step(conn, run_id)
    assert (step.status, step.input_tokens, step.output_tokens) == ("error", 7, 9)
    assert step.detail == {"error": "gone", "kind": "unavailable"}
    assert blocks == [TextBlock(text="par")]


async def test_a_subagents_model_call_that_is_cancelled_keeps_what_had_come(conn):
    registry.override("abliteration", Turns("abliteration", [TextDelta("par"), Usage(7, 9), Stop("end"), asyncio.CancelledError()]))
    run_id, parent = await a_step(conn)
    with pytest.raises(asyncio.CancelledError):
        await model_call(conn, run_id, parent, [], set())
    step = await the_step(conn, run_id)
    assert (step.status, step.input_tokens, step.output_tokens, step.detail) == ("cancelled", 7, 9, {"stop_reason": "end"})


async def test_a_subagents_model_call_that_breaks_in_another_way_keeps_what_had_come(conn):
    registry.override("abliteration", Turns("abliteration", [Usage(2, 1), Stop("end"), RuntimeError("boom")]))
    run_id, parent = await a_step(conn)
    with pytest.raises(RuntimeError):
        await model_call(conn, run_id, parent, [], set())
    step = await the_step(conn, run_id)
    assert (step.status, step.input_tokens, step.output_tokens, step.detail) == ("error", 2, 1, {"stop_reason": "end"})


async def test_a_subagents_model_call_without_a_stop_reason_records_no_detail(conn):
    class Bare(Turns):
        async def stream(self, model, system, messages, options, tools=None):
            yield TextDelta("done")

    registry.override("abliteration", Bare("abliteration"))
    run_id, parent = await a_step(conn)
    assert await model_call(conn, run_id, parent, [], set()) == []
    step = await the_step(conn, run_id)
    assert (step.status, step.input_tokens, step.output_tokens, step.detail) == ("done", 0, 0, {})
