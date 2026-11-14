import re

import pytest

from hunterai.providers import base
from hunterai.providers.base import (Options, Stop, TextDelta, ThinkingDelta, ToolCall, Usage,
                                     WireMessage)
from hunterai.providers.mock import MockProvider
from hunterai.types import TextBlock, ThinkingBlock, ToolResultBlock
from tests.toolkit import SPEC, Turns, call, result, run_mock


def test_rounds_split_pair_and_fill_missing_results():
    m = WireMessage("assistant", [ThinkingBlock(text="t"), TextBlock(text="a"), call("c1", "read"),
                                  call("c2", "read"), result("c2", "r2"), result("c1", "r1"),
                                  result("ghost", "x"), TextBlock(text="b"), call("c3", "execute")])
    r1, r2 = base.rounds(m)
    assert [b.type for b in r1.blocks] == ["thinking", "text", "tool_call", "tool_call"]
    assert [(r.tool_call_id, r.content) for r in r1.results] == [("c1", "r1"), ("c2", "r2")]
    assert [b.type for b in r2.blocks] == ["text", "tool_call"]
    assert r2.results == [ToolResultBlock(tool_call_id="c3", content=base.NO_RESULT, is_error=True)]
    assert base.rounds(WireMessage("assistant", [TextBlock(text="x")]))[0].results == []


@pytest.mark.parametrize("raw,args,err", [
    (None, {}, None), ("", {}, None), ({"a": 1}, {"a": 1}, None), ('{"a": 1}', {"a": 1}, None),
    ('{"a":', {}, "not valid JSON"), ("[1]", {}, "must be a JSON object"),
    ('"s"', {}, "must be a JSON object")])
def test_parse_arguments(raw, args, err):
    got, e = base.parse_arguments(raw)
    assert got == args and (e is None if err is None else err in e)


async def test_mock_calls_then_reads_the_result():
    first = await run_mock('call execute {"command": "id"}', tools=[SPEC])
    assert first == [ToolCall("call_1", "execute", {"command": "id"}), Usage(3, 5), Stop("tool_calls")]
    again = await run_mock('call execute {"command": "id"}', tools=[SPEC],
                           after=[call("call_1", "execute", command="id"), result("call_1", "uid=1000")])
    assert again[0] == TextDelta("tool said: uid=1000")


async def test_mock_ignores_tool_scripts_without_tools():
    events = await run_mock("call execute {}", tools=None)
    assert events == [ThinkingDelta("thinking"), TextDelta("echo: call execute {}"), Usage(3, 5),
                      Stop("end")]


async def test_mock_bad_and_loop_scripts():
    bad = await run_mock("call-bad execute", tools=[SPEC])
    assert bad[0].error == "arguments are not valid JSON: mock" and bad[0].arguments == {}
    assert bad[0].name == "execute" and bad[-1] == Stop("tool_calls")
    one = await run_mock('loop execute {"command": "id"}', tools=[SPEC])
    assert one[0] == ToolCall("call_1", "execute", {"command": "id"})
    two = await run_mock('loop execute {"command": "id"}', tools=[SPEC],
                         after=[call("call_1", "execute", command="id"), result("call_1", "x")])
    assert two[0].id == "call_2" and two[-1] == Stop("tool_calls")


async def test_toolcall_and_stream_defaults_keep_phase1_shape():
    assert ToolCall("i", "n", {}) == ToolCall("i", "n", {}, None, None)
    msgs = [WireMessage("user", [TextBlock(text="hi")])]
    events = [e async for e in MockProvider().stream("mock-1", "", msgs, Options())]
    assert events[1] == TextDelta("echo: hi")


async def test_turns_replays_scripts_and_records_calls():
    p = Turns("x", [ToolCall("c1", "execute", {"command": "id"}), Stop("tool_calls")],
              [TextDelta("a"), RuntimeError("mid")], ValueError("start"))
    msgs = [WireMessage("user", [TextBlock(text="hi")])]
    first = [e async for e in p.stream("m", "s", msgs, Options(), tools=[SPEC])]
    assert first == [ToolCall("c1", "execute", {"command": "id"}), Stop("tool_calls"), Usage(3, 5)]
    assert p.calls[0]["tools"] == [SPEC] and p.calls[0]["system"] == "s"
    with pytest.raises(RuntimeError):
        [e async for e in p.stream("m", "s", msgs, Options())]
    with pytest.raises(ValueError):
        [e async for e in p.stream("m", "s", msgs, Options())]
    last = [e async for e in p.stream("m", "s", msgs, Options())]
    assert last == [TextDelta("done"), Usage(3, 5), Stop("end")]


@pytest.mark.parametrize("raw", [None, "", "../x", "a/b", "id with space", "x" * 65, "call_1\n"])
def test_unsafe_ids_are_replaced(raw):
    assert re.fullmatch(r"call_[A-Za-z0-9]{16}", base.safe_call_id(raw))


def test_safe_ids_are_kept_unless_taken():
    assert base.safe_call_id("toolu_01Ab-9") == "toolu_01Ab-9"
    assert base.safe_call_id("call_1", taken={"call_1"}) not in ("call_1", "")
