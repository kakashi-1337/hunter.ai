import json

import httpx
import pytest
import respx

from hunterai.providers import base, openai_compat
from hunterai.providers.base import (Options, Stop, TextDelta, ToolCall, Usage, WireMessage)
from hunterai.types import TextBlock, ThinkingBlock
from tests.toolkit import SPEC, call, result

URL = "https://api.abliteration.ai/v1/chat/completions"


def sse(*chunks):
    lines = [": keep-alive", ""]
    for c in chunks:
        lines += ["data: " + json.dumps(c), ""]
    lines += ["data: [DONE]", ""]
    return "\n".join(lines)


BODY = sse(
    {"choices": [{"delta": {"content": "Hello"}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 4}})

CALLS = sse(
    {"choices": [{"delta": {"role": "assistant", "tool_calls": [
        {"index": 0, "id": "call_a", "type": "function",
         "function": {"name": "execute", "arguments": ""}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"comm"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "and\": \"id\"}"}},
        {"index": 1, "id": "call_b", "function": {"name": "read", "arguments": "{}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 9}})


def one_call(**tc):
    return sse({"choices": [{"delta": {"tool_calls": [{"index": 0, **tc}]},
                             "finish_reason": "tool_calls"}]},
               {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


@pytest.fixture
def ablit(monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "ak_test")
    return openai_compat.abliteration()


def user(*blocks):
    return [WireMessage(role="user", blocks=list(blocks))]


def sent(route):
    return json.loads(route.calls.last.request.content)


async def run(p, body, tools=(SPEC,), msgs=None):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=body))
    evs = [e async for e in p.stream("abliterated-model", "sys", msgs or user(TextBlock(text="hi")),
                                     Options(), tools=list(tools) if tools is not None else None)]
    return evs, route


@respx.mock
async def test_fragmented_parallel_calls_are_assembled(ablit):
    evs, route = await run(ablit, CALLS)
    assert evs == [ToolCall("call_a", "execute", {"command": "id"}), ToolCall("call_b", "read", {}),
                   Usage(20, 9), Stop("tool_calls")]
    assert sent(route)["tools"] == [{"type": "function", "function": {
        "name": "execute", "description": "Run a command", "parameters": SPEC.parameters}}]
    assert "tool_choice" not in sent(route) and "parallel_tool_calls" not in sent(route)


@respx.mock
async def test_tool_round_is_sent_back_in_openai_shape(ablit):
    msgs = user(TextBlock(text="hi")) + [WireMessage("assistant", [
        ThinkingBlock(text="t"), TextBlock(text="let me"), call("call_a", "execute", command="id"),
        result("call_a", "uid=1000"), call("call_c", "read", path="/x")])]
    _, route = await run(ablit, BODY, msgs=msgs)
    m = sent(route)["messages"][2:]
    assert m[0]["role"] == "assistant" and m[0]["content"] == "let me"
    assert m[0]["tool_calls"][0]["id"] == "call_a" and m[0]["tool_calls"][0]["type"] == "function"
    assert json.loads(m[0]["tool_calls"][0]["function"]["arguments"]) == {"command": "id"}
    assert m[1] == {"role": "tool", "tool_call_id": "call_a", "content": "uid=1000"}
    assert "content" not in m[2] and m[2]["tool_calls"][0]["function"]["name"] == "read"
    assert m[3] == {"role": "tool", "tool_call_id": "call_c", "content": base.NO_RESULT}
    assert len(m) == 4 and "reasoning" not in json.dumps(m)


@respx.mock
async def test_plain_assistant_message_keeps_the_phase1_shape(ablit):
    msgs = user(TextBlock(text="hi")) + [WireMessage("assistant", [TextBlock(text="yo")])] \
        + user(TextBlock(text="again"))
    _, route = await run(ablit, BODY, msgs=msgs)
    assert sent(route)["messages"][2] == {"role": "assistant", "content": "yo"}


@respx.mock
async def test_malformed_arguments_become_an_error_call(ablit):
    evs, _ = await run(ablit, one_call(id="call_a", function={"name": "execute",
                                                              "arguments": '{"command":'}))
    assert evs[0].id == "call_a" and evs[0].name == "execute" and evs[0].arguments == {}
    assert "not valid JSON" in evs[0].error and evs[1:] == [Usage(1, 1), Stop("tool_calls")]


@respx.mock
async def test_call_without_id_or_name(ablit):
    evs, _ = await run(ablit, one_call(function={"name": "execute", "arguments": "{}"}))
    assert evs[0].id.startswith("call_") and evs[0].error is None
    evs, _ = await run(ablit, one_call(id="../x", function={"name": "execute", "arguments": "{}"}))
    assert evs[0].id.startswith("call_") and "/" not in evs[0].id
    evs, _ = await run(ablit, one_call(id="call_a", function={"arguments": "{}"}))
    assert evs[0] == ToolCall("call_a", "", {}, error="tool call without a name")


@respx.mock
async def test_no_tools_key_without_tools(ablit):
    for tools in (None, ()):
        _, route = await run(ablit, BODY, tools=tools)
        assert "tools" not in sent(route)


@respx.mock
async def test_text_and_calls_in_one_turn_keep_order(ablit):
    body = sse({"choices": [{"delta": {"content": "checking"}}]},
               {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_a", "function": {
                   "name": "execute", "arguments": "{\"command\": \"id\"}"}}]}}]},
               {"choices": [{"delta": {}, "finish_reason": "stop"}]},
               {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})
    evs, _ = await run(ablit, body)
    assert evs == [TextDelta("checking"), ToolCall("call_a", "execute", {"command": "id"}),
                   Usage(2, 3), Stop("stop")]
