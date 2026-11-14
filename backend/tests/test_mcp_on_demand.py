import asyncio
import dataclasses

import pytest

from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import chats, settings
from hunterai.tools import on_demand, registry, runner
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, answer_next, fake_tool, run_call, seeded_run

SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False}


@pytest.fixture
def servers():
    """A big MCP server (20 tools), a small one (3), and one native tool; the handlers' calls by tool name."""
    calls = {}
    for t in on_demand.tools():
        registry.register(t)
    for i in range(20):
        tool, ran = fake_tool(f"big_tool_{i:02d}", risk="read", text=f"big {i}", schema=SCHEMA, source="mcp:big")
        tool = dataclasses.replace(tool, description=f"Decompile function number {i}" if i < 19 else "List the strings of a binary")
        registry.register(tool); calls[tool.name] = ran
    for i in range(3):
        tool, ran = fake_tool(f"small_tool_{i}", source="mcp:small")
        registry.register(tool); calls[tool.name] = ran
    tool, ran = fake_tool("t_native")
    registry.register(tool); calls["t_native"] = ran
    return calls


async def split(conn, chat):
    return await runner.for_model(conn, await runner.offered_tools(conn, await chats.get(conn, chat.id)))


async def test_a_big_server_is_kept_out_of_the_tool_list_and_reachable_through_two_tools(conn, servers):
    chat, _ = await seeded_run(conn)
    offered = await runner.offered_tools(conn, await chats.get(conn, chat.id))
    assert not {on_demand.FIND, on_demand.CALL} & {t.name for t in offered}          # never offered by themselves
    shown, deferred = await runner.for_model(conn, offered)
    assert sorted(t.name for t in shown) == ["mcp_call", "mcp_find", "small_tool_0", "small_tool_1", "small_tool_2", "t_native"]
    assert [t.name for t in deferred] == [f"big_tool_{i:02d}" for i in range(20)]
    index = on_demand.index(deferred)
    assert "big (20 tools)" in index and "big_tool_07" in index and "mcp_find" in index and "mcp_call" in index
    # The owner switched it off: everything is listed as before, and the two helper tools are not there.
    await settings.put(conn, "mcp_on_demand", False)
    shown, deferred = await split(conn, chat)
    assert len(shown) == 24 and deferred == [] and "mcp_find" not in {t.name for t in shown}


async def test_finding_a_tool_gives_its_description_and_arguments(conn, servers):
    chat, run_id = await seeded_run(conn)
    names = {t.name for part in await split(conn, chat) for t in part}
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", "mcp_find", {"query": "strings of a binary"}), offered=names)
    text = out.block.content
    assert not out.block.is_error and "## big_tool_19" in text and "big_tool_00" not in text      # what matches, not everything
    assert "List the strings of a binary" in text and '"required": ["path"]' in text and "mcp_call" in text
    assert text.count("\n## ") <= on_demand.MAX_FOUND
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c2", "mcp_find", {"query": "big_tool_03"}), offered=names)
    assert out.block.content.index("big_tool_03") < 200
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c3", "mcp_find", {"query": "kubernetes"}), offered=names)
    assert "No tool matches" in out.block.content and "big (20 tools)" in out.block.content
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c4", "mcp_find", {"query": "tool", "server": "small"}), offered=names)
    assert "small" in out.block.content and "not loaded on demand" in out.block.content


async def test_a_call_through_mcp_call_is_the_tool_itself_for_every_rule(conn, servers):
    chat, run_id = await seeded_run(conn, permissions={"risk": {"read": "allow"}})
    names = {t.name for part in await split(conn, chat) for t in part}
    out, _, steps = await run_call(conn, chat, run_id, ToolCall("c1", "mcp_call", {"tool": "big_tool_05", "arguments": {"path": "/bin/ls"}}), offered=names)
    assert out.block.content == "big 5" and servers["big_tool_05"] == [{"path": "/bin/ls"}]
    assert [s.name for s in steps if s.kind == "tool_call"] == ["big_tool_05"]                # recorded under its own name
    # Its own schema is checked, with the schema in the error so the model can correct itself.
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c2", "mcp_call", {"tool": "big_tool_05", "arguments": {"file": "x"}}), offered=names)
    assert out.block.is_error and "path" in out.block.content and servers["big_tool_05"] == [{"path": "/bin/ls"}]
    # Only tools that are loaded on demand: not a native tool, not an unknown name.
    for tool in ("t_native", "nope", "mcp_call"):
        out, _, _ = await run_call(conn, chat, run_id, ToolCall("c3", "mcp_call", {"tool": tool, "arguments": {}}), offered=names)
        assert out.block.is_error and "mcp_find" in out.block.content
    assert servers["t_native"] == []
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c4", "mcp_call", {"arguments": {}}), offered=names)
    assert out.block.is_error


async def test_the_owner_is_asked_about_the_real_tool(conn, servers):
    tool, ran = fake_tool("big_danger", risk="execute", source="mcp:big"); registry.register(tool)
    chat, run_id = await seeded_run(conn)
    names = {t.name for part in await split(conn, chat) for t in part}
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "mcp_call", {"tool": "big_danger", "arguments": {}}), offered=names))
    req = await answer_next(run_id, "deny")
    out, _, _ = await task
    assert req.tool == "big_danger" and req.risk == "execute" and ran == [] and "denied" in out.block.content


async def test_the_model_gets_the_short_list_and_the_index(conn, setup, servers):
    await settings.put(conn, "permissions", {"risk": {"read": "allow"}})
    p = Turns("abliteration", [ToolCall("c1", "mcp_call", {"tool": "big_tool_02", "arguments": {"path": "a"}})], [TextDelta("done")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and servers["big_tool_02"] == [{"path": "a"}]
    sent = [t.name for t in p.calls[0]["tools"]]
    assert "mcp_call" in sent and "mcp_find" in sent and "small_tool_1" in sent and not [n for n in sent if n.startswith("big_")]
    assert "big (20 tools)" in p.calls[0]["system"] and "big_tool_02" in p.calls[0]["system"]
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_result", "text"] and msg.blocks[0].name == "mcp_call"
