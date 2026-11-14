from types import SimpleNamespace as NS

import pytest

from hunterai.providers import base
from hunterai.providers.anthropic import AnthropicProvider
from hunterai.providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                                     ThinkingSeal, ToolCall, Usage, WireMessage)
from hunterai.types import TextBlock, ThinkingBlock
from tests.test_anthropic import FakeClient, delta
from tests.toolkit import SPEC, call, result

M = "claude-opus-5-5"


def user(*blocks):
    return [WireMessage(role="user", blocks=list(blocks))]


def stop(block):
    return NS(type="content_block_stop", content_block=block)


async def run(c, msgs=None, model=M):
    return [e async for e in AnthropicProvider(client=c).stream(
        model, "sys", msgs or user(TextBlock(text="hi")), Options(), tools=[SPEC])]


B1 = {"type": "thinking", "thinking": "plan", "signature": "sig1"}
TOOL_USE = stop(NS(type="tool_use", id="toolu_1", name="execute", input={"command": "id"}))
TOOL_EVENTS = [NS(type="message_start"), delta("thinking_delta", thinking="plan"), stop(NS(**B1)),
               stop(NS(type="redacted_thinking", data="ENC")),
               delta("text_delta", text="Running."), stop(NS(type="text", text="Running.")),
               TOOL_USE, NS(type="message_stop")]


async def test_tool_use_and_thinking_seals_are_emitted_in_order():
    c = FakeClient(events=TOOL_EVENTS, stop="tool_use")
    evs = await run(c)
    assert evs == [
        ThinkingDelta("plan"), ThinkingSeal({"provider": "anthropic", "model": M, "block": B1}),
        ThinkingSeal({"provider": "anthropic", "model": M,
                      "block": {"type": "redacted_thinking", "data": "ENC"}}),
        TextDelta("Running."), ToolCall("toolu_1", "execute", {"command": "id"}), Usage(12, 7),
        Stop("tool_use")]
    kw = c.calls[0][1]
    assert kw["tools"] == [{"name": "execute", "description": "Run a command",
                            "input_schema": SPEC.parameters, "eager_input_streaming": True}]
    assert "tool_choice" not in kw


async def test_thinking_is_replayed_only_for_the_run_in_progress():
    seal = {"provider": "anthropic", "model": M, "block": B1}
    old = WireMessage("assistant", [
        ThinkingBlock(text="old", opaque={**seal, "block": {**B1, "signature": "old"}}),
        TextBlock(text="look"), call("toolu_0", "read", path="/x"), result("toolu_0", "data"),
        TextBlock(text="done")])
    live = WireMessage("assistant", [ThinkingBlock(text="plan", opaque=seal),
                                     TextBlock(text="Running."),
                                     call("toolu_1", "execute", command="id"),
                                     result("toolu_1", "uid=1000")])
    c = FakeClient()
    await run(c, user(TextBlock(text="hi")) + [old] + user(TextBlock(text="again")) + [live])
    sent = c.calls[0][1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user", "assistant", "user",
                                         "assistant", "user"]
    assert [b["type"] for b in sent[1]["content"]] == ["text", "tool_use"]   # earlier run: no thinking
    assert sent[5]["content"] == [B1, {"type": "text", "text": "Running."},
                                  {"type": "tool_use", "id": "toolu_1", "name": "execute",
                                   "input": {"command": "id"}}]
    assert sent[6]["content"] == [{"type": "tool_result", "tool_use_id": "toolu_1",
                                   "content": "uid=1000", "is_error": False}]


async def test_dangling_call_and_next_user_text_share_one_user_message():
    c = FakeClient()
    msgs = user(TextBlock(text="hi")) + [WireMessage("assistant", [call("c9", "read", path="/x")])] \
        + user(TextBlock(text="go on"))
    await run(c, msgs)
    sent = c.calls[0][1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c9", "content": base.NO_RESULT, "is_error": True},
        {"type": "text", "text": "go on"}]


async def test_foreign_thinking_is_never_sent():
    live = WireMessage("assistant", [ThinkingBlock(text="a"),
                                     ThinkingBlock(text="b", opaque={"provider": "google"}),
                                     call("c1", "read", path="/x"), result("c1", "ok")])
    c = FakeClient()
    await run(c, user(TextBlock(text="hi")) + [live])
    assert [b["type"] for b in c.calls[0][1]["messages"][1]["content"]] == ["tool_use"]


async def test_max_tokens_marks_calls_cut_off():
    evs = await run(FakeClient(events=[TOOL_USE], stop="max_tokens"))
    assert "cut off" in evs[0].error and evs[0].arguments == {} and evs[-1] == Stop("max_tokens")


async def test_non_object_input_is_an_error_call():
    bad = stop(NS(type="tool_use", id="toolu_1", name="execute", input="oops"))
    evs = await run(FakeClient(events=[bad], stop="tool_use"))
    assert evs[0] == ToolCall("toolu_1", "execute", {}, error="arguments must be a JSON object")


async def test_refusal_yields_no_tool_calls():
    with pytest.raises(ProviderError) as e:
        await run(FakeClient(events=[TOOL_USE], stop="refusal"))
    assert e.value.kind == "policy"


async def test_unparseable_tool_input_becomes_an_error_call():
    start = NS(type="content_block_start",
               content_block=NS(type="tool_use", id="toolu_1", name="execute"))
    c = FakeClient(events=[NS(type="message_start"), start], error=ValueError("bad json"))
    evs = await run(c)
    assert evs[0] == ToolCall("toolu_1", "execute", {},
                              error="tool input was not valid JSON; send the call again")
    assert evs[1:] == [Usage(0, 0), Stop("tool_use")]            # nothing raised
    c = FakeClient(events=[NS(type="message_start")], error=ValueError("bad json"))
    with pytest.raises(ProviderError) as e:                      # no tool_use block had started
        await run(c)
    assert e.value.kind == "bad_request"


async def test_both_api_paths_carry_tools():
    c = FakeClient()
    await run(c)
    await run(c, model="claude-haiku-5-5")
    assert [label for label, _ in c.calls] == ["beta", "stable"]
    assert all("tools" in kw for _, kw in c.calls)
