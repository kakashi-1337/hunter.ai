import json

import httpx
import pytest
import respx

from hunterai.providers import ollama
from hunterai.providers.base import Options, ProviderError, Stop, ThinkingDelta, ToolCall, Usage, WireMessage
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
    respx.post(URL.replace("/api/chat", "/api/show")).mock(return_value=httpx.Response(200, json={"capabilities": ["completion", "tools"]}))
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
    assert sent(route)["options"]["num_ctx"] == ollama.DEFAULT_NUM_CTX


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


# ---- a tool call that the limit on a call's length cut

# Measured on an Ollama built from source on 27 November 2026. A call whose text or thinking reaches num_predict ends
# with done_reason "length". One whose tool call reaches it has JSON without an end, and Ollama says that as an error:
# HTTP 500 when it had sent nothing yet, a line of the stream when thinking came first.
CUT = 'llama-server returned invalid tool call arguments for "execute": unexpected end of JSON input'


async def limited(max_tokens: int | None = 4096):
    respx.post(URL.replace("/api/chat", "/api/show")).mock(return_value=httpx.Response(200, json={"capabilities": ["completion", "tools"]}))
    return [e async for e in ollama.make().stream("qwen3:8b", "sys", HI, Options(max_tokens=max_tokens), tools=[SPEC])]


@respx.mock
@pytest.mark.parametrize("answer", [httpx.Response(500, json={"error": CUT}), httpx.Response(500, text=CUT)])
async def test_a_tool_call_that_the_limit_of_length_cut_is_the_end_of_the_call_at_that_limit(answer):
    route = respx.post(URL).mock(return_value=answer)
    assert await limited() == [Usage(0, 0), Stop("length")]
    assert sent(route)["options"]["num_predict"] == 4096


@respx.mock
async def test_after_thinking_the_same_words_come_as_a_line_of_the_stream():
    respx.post(URL).mock(return_value=httpx.Response(200, text=ndjson(
        {"message": {"role": "assistant", "thinking": "The user"}, "done": False}, {"error": CUT})))
    assert await limited() == [ThinkingDelta("The user"), Usage(0, 0), Stop("length")]


@respx.mock
@pytest.mark.parametrize("answer", [
    httpx.Response(500, json={"error": CUT}),
    httpx.Response(200, text=ndjson({"message": {"role": "assistant", "thinking": "The user"}, "done": False}, {"error": CUT})),
])
async def test_without_a_limit_those_words_are_the_error_they_were(answer):
    respx.post(URL).mock(return_value=answer)
    with pytest.raises(ProviderError) as e:
        await limited(None)
    assert "invalid tool call arguments" in e.value.message


@respx.mock
@pytest.mark.parametrize("answer", [
    httpx.Response(500, json={"error": "llama-server process has terminated"}),
    httpx.Response(500, json={"error": 'llama-server returned invalid tool call arguments for "execute": invalid character'}),
    httpx.Response(200, text=ndjson({"message": {"role": "assistant", "thinking": "The user"}, "done": False},
                                    {"error": "out of memory"})),
])
async def test_another_error_is_an_error_under_a_limit_too(answer):
    respx.post(URL).mock(return_value=answer)
    with pytest.raises(ProviderError):
        await limited()
