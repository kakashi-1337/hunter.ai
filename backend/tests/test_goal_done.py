import json

from hunterai.providers.base import ToolCall
from hunterai.repo import goals
from hunterai.tools import mind, registry
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)
from tests.toolkit import run_call, seeded_run


async def focus(conn, chat, run_id, n, **args):
    out, _, _ = await run_call(conn, chat, run_id, ToolCall(f"c{n}", "focus", args))
    assert not out.block.is_error, out.block.content
    return await goals.get(conn, chat.id)


async def test_an_item_does_not_become_done_on_the_agents_word(conn):
    """It did, once: whatever left the open list was shown as done. Now a tool result has to show the work
    (tests/test_goal_check.py); in a chat with no tool result nothing can."""
    for t in mind.tools():
        registry.register(t)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"read": "allow", "write": "allow"}})
    g = await focus(conn, chat, run_id, 1, action="set", objective="Map the API", success_criteria=["every endpoint listed"],
                    open_items=["crawl the site", "read the JS bundles", "list endpoints"])
    assert g.open_items == ["crawl the site", "read the JS bundles", "list endpoints"] and g.done_items == []
    # Said to be done: it stays open. What is added is added.
    g = await focus(conn, chat, run_id, 2, action="update", done=["crawl the site"], add_open=["check auth on each endpoint"])
    assert g.open_items == ["crawl the site", "read the JS bundles", "list endpoints", "check auth on each endpoint"]
    assert g.done_items == [] and g.marks["crawl the site"].verdict == "not_shown"
    # The whole list replaced: what is no longer on it is gone, and not done.
    g = await focus(conn, chat, run_id, 3, action="update", open_items=["check auth on each endpoint"])
    assert g.open_items == ["check auth on each endpoint"] and g.done_items == [] and g.marks == {}
    # A new goal starts with nothing done.
    g = await focus(conn, chat, run_id, 4, action="set", objective="Something else", open_items=["one"])
    assert (g.objective, g.open_items, g.done_items) == ("Something else", ["one"], [])


async def test_the_goal_route_gives_the_done_items(client):
    chat = await new_chat(client)
    from hunterai import db
    conn = await db.connect()
    try:
        await goals.put(conn, chat["id"], "Find an IDOR", ["one"], ["two"], ["zero"])
    finally:
        await conn.close()
    goal = (await client.get(f"/api/chats/{chat['id']}/goal")).json()
    assert (goal["open_items"], goal["done_items"]) == (["two"], ["zero"])
