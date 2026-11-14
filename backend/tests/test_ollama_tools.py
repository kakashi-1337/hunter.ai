import json

import httpx
import pytest
import respx

from hunterai.providers import ollama
from hunterai.providers.base import Options, ProviderError, Stop, ToolCall, Usage, WireMessage
from hunterai.types import TextBlock
from tests.toolkit import SPEC, call, result

URL = "http://127.0.0.1:11434/api/chat"


def ndjson(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


def sent(route):
    return json.loads(route.calls.last.request.content)


DONE = {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
        "prompt_eval_count": 9, "eval_count": 2}
BODY = ndjson({"message": {"role": "assistant", "content": "Hi"}, "done": False}, DONE)
TOOL_BODY = ndjson(
    {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "execute", "arguments": {"command": "id"}}}]}, "done": False},
    DONE)
HI = [WireMessage("user", [TextBlock(text="hi")])]


async def run(msgs=HI, tools=(SPEC,), system="sys"):
    return [e async for e in ollama.make().stream(
        "qwen3:8b", system, msgs, Options(), tools=list(tools) if tools is not None else None)]


@respx.mock
async def test_tool_call_parsed_and_tools_sent():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=TOOL_BODY))
    evs = await run()
    assert evs[0].name == "execute" and evs[0].arguments == {"command": "id"}
    assert evs[0].id.startswith("call_") and evs[1:] == [Usage(9, 2), Stop("stop")]
    assert sent(route)["tools"] == [{"type": "function", "function": {
        "name": "execute", "description": "Run a command", "parameters": SPEC.parameters}}]
    assert sent(route)["options"]["num_ctx"] == 8192


@respx.mock
async def test_tool_round_is_sent_back_in_ollama_shape():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    msgs = HI + [WireMessage("assistant", [call("c1", "execute", command="id"),
                                           result("c1", "uid=1000")])]
    await run(msgs, system="")
    assert sent(route)["messages"][1:] == [
        {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {
            "index": 0, "name": "execute", "arguments": {"command": "id"}}}]},
        {"role": "tool", "tool_name": "execute", "content": "uid=1000"}]


@respx.mock
async def test_string_arguments_and_given_id_are_accepted():
    body = ndjson({"message": {"tool_calls": [
        {"id": "x9", "function": {"name": "t", "arguments": '{"a": 1}'}},
        {"id": "../x", "function": {"name": "t", "arguments": {}}}]}, "done": False}, DONE)
    respx.post(URL).mock(return_value=httpx.Response(200, text=body))
    evs = await run()
    assert evs[0] == ToolCall("x9", "t", {"a": 1})
    assert evs[1].id.startswith("call_") and "/" not in evs[1].id


@respx.mock
async def test_model_without_tool_support_is_a_bad_request():
    respx.post(URL).mock(return_value=httpx.Response(
        400, json={"error": "registry.ollama.ai/x does not support tools"}))
    with pytest.raises(ProviderError) as e:
        await run()
    assert e.value.kind == "bad_request" and "qwen3:8b does not support tools" in e.value.message


@respx.mock
async def test_no_tools_key_without_tools():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    for tools in (None, ()):
        await run(tools=tools)
        assert "tools" not in sent(route)
