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


async def test_items_that_leave_the_open_list_are_kept_as_done(conn):
    for t in mind.tools():
        registry.register(t)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"read": "allow", "write": "allow"}})
    g = await focus(conn, chat, run_id, 1, action="set", objective="Map the API", success_criteria=["every endpoint listed"],
                    open_items=["crawl the site", "read the JS bundles", "list endpoints"])
    assert g.open_items == ["crawl the site", "read the JS bundles", "list endpoints"] and g.done_items == []
    g = await focus(conn, chat, run_id, 2, action="update", done=["crawl the site"], add_open=["check auth on each endpoint"])
    assert g.open_items == ["read the JS bundles", "list endpoints", "check auth on each endpoint"] and g.done_items == ["crawl the site"]
    # The whole list replaced: what is no longer on it counts as done too, in the order it left.
    g = await focus(conn, chat, run_id, 3, action="update", open_items=["check auth on each endpoint"])
    assert g.open_items == ["check auth on each endpoint"] and g.done_items == ["crawl the site", "read the JS bundles", "list endpoints"]
    # An item that comes back is open again, not both.
    g = await focus(conn, chat, run_id, 4, action="update", add_open=["list endpoints"])
    assert "list endpoints" in g.open_items and "list endpoints" not in g.done_items
    # A new goal starts with nothing done.
    g = await focus(conn, chat, run_id, 5, action="set", objective="Something else", open_items=["one"])
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
