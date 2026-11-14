import inspect
from types import SimpleNamespace as NS

from hunterai.providers import codex
from hunterai.providers.base import Options, Stop, TextDelta, WireMessage
from hunterai.providers.google import GoogleProvider
from hunterai.types import TextBlock, ToolCallBlock
from tests.test_google import FakeClient, user
from tests.toolkit import SPEC, call, result


def fc_chunk(name, args, sig=None, text=None):
    part = NS(text=text, thought=None, function_call=NS(name=name, args=args, id=None),
              thought_signature=sig)
    return NS(candidates=[NS(content=NS(parts=[part]), finish_reason=NS(name="STOP"))],
              usage_metadata=None)


async def run(c, msgs=None, tools=(SPEC,), system="sys"):
    return [e async for e in GoogleProvider(client=c).stream(
        "gemini-x", system, msgs or user(TextBlock(text="hi")), Options(),
        tools=list(tools) if tools is not None else None)]


async def test_function_call_becomes_a_tool_call_with_signature():
    c = FakeClient(chunks=[fc_chunk("execute", {"command": "id"}, sig=b"\x01\x02")])
    evs = await run(c)
    assert (evs[0].name, evs[0].arguments) == ("execute", {"command": "id"})
    assert evs[0].id.startswith("call_")
    assert evs[0].opaque == {"provider": "google", "model": "gemini-x", "thought_signature": "AQI="}
    cfg = c.calls[0]["config"]
    assert cfg["tools"] == [{"function_declarations": [{
        "name": "execute", "description": "Run a command",
        "parameters_json_schema": SPEC.parameters}]}]
    assert cfg["automatic_function_calling"] == {"disable": True}


async def test_function_call_without_signature_has_no_opaque():
    evs = await run(FakeClient(chunks=[fc_chunk("execute", None)]))
    assert evs[0].opaque is None and evs[0].arguments == {} and evs[-1] == Stop("STOP")


async def test_tool_round_is_sent_back_in_gemini_shape():
    c = FakeClient()
    blocks = [TextBlock(text="let me"),
              ToolCallBlock(id="c1", name="execute", arguments={"command": "id"},
                            opaque={"provider": "google", "model": "gemini-x",
                                    "thought_signature": "AQI="}),
              result("c1", "uid=1000"), call("c2", "read", path="/x"),
              result("c2", "nope", is_error=True)]
    await run(c, user(TextBlock(text="hi")) + [WireMessage("assistant", blocks)], system="")
    sent = c.calls[0]["contents"]
    assert sent[1] == {"role": "model", "parts": [
        {"text": "let me"},
        {"function_call": {"name": "execute", "args": {"command": "id"}},
         "thought_signature": b"\x01\x02"}]}
    assert sent[2] == {"role": "user", "parts": [
        {"function_response": {"name": "execute", "response": {"output": "uid=1000"}}}]}
    assert sent[3]["parts"] == [{"function_call": {"name": "read", "args": {"path": "/x"}}}]
    assert sent[4]["parts"] == [
        {"function_response": {"name": "read", "response": {"error": "nope"}}}]


async def test_foreign_opaque_is_not_sent_as_a_signature():
    c = FakeClient()
    blocks = [ToolCallBlock(id="c1", name="execute", arguments={}, opaque={"provider": "anthropic"}),
              result("c1", "ok")]
    await run(c, user(TextBlock(text="hi")) + [WireMessage("assistant", blocks)])
    assert c.calls[0]["contents"][1]["parts"] == [{"function_call": {"name": "execute", "args": {}}}]


async def test_no_tool_config_without_tools():
    for tools in (None, ()):
        c = FakeClient()
        evs = await run(c, tools=tools)
        assert "tools" not in c.calls[0]["config"]
        assert "automatic_function_calling" not in c.calls[0]["config"]
        assert TextDelta("Hel") in evs


def test_codex_declares_no_tool_support():
    assert not getattr(codex.make(), "supports_tools", False)
    assert list(inspect.signature(codex.CodexProvider.stream).parameters) == [
        "self", "model", "system", "messages", "options"]
    assert "No tools" in codex.__doc__
