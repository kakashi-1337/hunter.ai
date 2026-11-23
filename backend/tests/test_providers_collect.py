"""One helper reads what a model call sends into blocks. The subagent loop and the bench both use it, so a call
is read the same way in both."""
import re

import pytest

from hunterai.providers.base import (
    ID_RE,
    ProviderError,
    Reply,
    Stop,
    TextDelta,
    ThinkingDelta,
    ThinkingSeal,
    ToolCall,
    Usage,
    collect,
)
from hunterai.types import TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock


async def sent(*events):
    for ev in events:
        if isinstance(ev, BaseException):
            raise ev
        yield ev


async def test_text_in_pieces_becomes_one_block():
    blocks = []
    await collect(sent(TextDelta("Hel"), TextDelta("lo"), TextDelta(".")), blocks, set())
    assert blocks == [TextBlock(text="Hello.")]


async def test_text_goes_on_in_a_text_block_that_is_the_last_one():
    blocks = [ThinkingBlock(text="t"), TextBlock(text="a")]
    await collect(sent(TextDelta("b")), blocks, set())
    assert blocks == [ThinkingBlock(text="t"), TextBlock(text="ab")]


async def test_text_after_a_tool_result_starts_a_new_block():
    before = [TextBlock(text="a"), ToolCallBlock(id="c1", name="x", arguments={}), ToolResultBlock(tool_call_id="c1", content="r")]
    blocks = list(before)
    await collect(sent(TextDelta("b")), blocks, {"c1"})
    assert blocks == before + [TextBlock(text="b")]


async def test_thinking_and_its_seal_stay_together():
    blocks = []
    await collect(sent(ThinkingDelta("a"), ThinkingDelta("b"), ThinkingSeal({"signature": "s"})), blocks, set())
    assert blocks == [ThinkingBlock(text="ab", opaque={"signature": "s"})]


async def test_a_seal_without_thinking_is_its_own_block():
    blocks = []
    await collect(sent(ThinkingSeal({"data": "x"})), blocks, set())
    assert blocks == [ThinkingBlock(text="", opaque={"data": "x"})]


async def test_thinking_after_a_seal_starts_a_new_block_and_a_second_seal_stands_alone():
    blocks = []
    await collect(sent(ThinkingDelta("a"), ThinkingSeal({"n": 1}), ThinkingDelta("b"), ThinkingSeal({"n": 2}), ThinkingSeal({"n": 3})),
                  blocks, set())
    assert blocks == [ThinkingBlock(text="a", opaque={"n": 1}), ThinkingBlock(text="b", opaque={"n": 2}),
                      ThinkingBlock(text="", opaque={"n": 3})]


async def test_the_blocks_keep_the_order_in_which_they_were_sent():
    blocks = []
    reply = await collect(sent(ThinkingDelta("think"), TextDelta("say"), ToolCall("c1", "read", {"path": "a"}), TextDelta("more"),
                               ThinkingDelta("again")), blocks, set())
    assert [(b.type, getattr(b, "text", None)) for b in blocks] == \
        [("thinking", "think"), ("text", "say"), ("tool_call", None), ("text", "more"), ("thinking", "again")]
    assert reply.calls == [ToolCall("c1", "read", {"path": "a"})]


async def test_a_call_gets_an_id_that_is_not_taken():
    blocks, taken = [], {"old"}
    reply = await collect(sent(ToolCall("call_1", "read", {"path": "a"}), ToolCall("call_1", "read", {"path": "b"}),
                               ToolCall("old", "read", {"path": "c"}), ToolCall("not an id!", "read", {"path": "d"})), blocks, taken)
    ids = [c.id for c in reply.calls]
    assert ids[0] == "call_1" and len(set(ids)) == 4 and "old" not in ids and "not an id!" not in ids
    assert all(re.fullmatch(ID_RE, i) for i in ids)
    assert taken == {"old", *ids}
    assert [(b.id, b.name, b.arguments) for b in blocks] == [(i, "read", {"path": p}) for i, p in zip(ids, "abcd")]
    assert [c.arguments for c in reply.calls] == [{"path": p} for p in "abcd"]


async def test_a_call_keeps_its_error_and_what_its_adapter_attached():
    blocks = []
    reply = await collect(sent(ToolCall("c1", "read", {}, error="the arguments were not valid JSON", opaque={"provider": "x"})),
                          blocks, set())
    assert reply.calls == [ToolCall("c1", "read", {}, error="the arguments were not valid JSON", opaque={"provider": "x"})]
    assert blocks == [ToolCallBlock(id="c1", name="read", arguments={}, opaque={"provider": "x"})]


async def test_usage_and_stop_reason_are_returned():
    reply = await collect(sent(TextDelta("x"), Usage(1, 1), Usage(3, 5), Stop("tool_calls"), Stop("end")), [], set())
    assert (reply.usage, reply.stop) == (Usage(3, 5), "end")    # the last of each


async def test_nothing_sent_returns_no_usage():
    blocks = [TextBlock(text="a")]
    reply = await collect(sent(), blocks, set())
    assert (reply.calls, reply.usage, reply.stop) == ([], Usage(0, 0), None)
    assert blocks == [TextBlock(text="a")]


async def test_what_came_before_a_failure_is_in_the_reply_that_was_handed_in():
    blocks, taken, reply = [], set(), Reply()
    with pytest.raises(ProviderError):
        await collect(sent(TextDelta("par"), ToolCall("c1", "read", {}), Usage(7, 9), Stop("end"),
                           ProviderError("unavailable", "gone")), blocks, taken, reply)
    assert (reply.usage, reply.stop, [c.id for c in reply.calls]) == (Usage(7, 9), "end", ["c1"])
    assert [b.type for b in blocks] == ["text", "tool_call"] and taken == {"c1"}


async def test_the_reply_that_was_handed_in_is_the_one_returned_and_a_new_one_is_made_each_time():
    mine = Reply()
    assert await collect(sent(Usage(1, 2)), [], set(), mine) is mine
    first = await collect(sent(ToolCall("c1", "read", {})), [], set())
    second = await collect(sent(), [], set())
    assert first is not second and second.calls == []    # no list shared between two calls
