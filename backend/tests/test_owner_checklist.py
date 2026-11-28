"""The owner's own items on a chat's goal record: he writes them, and only he takes one off."""
from hunterai import goal_check
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import goals
from hunterai.tools import registry
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.test_goal_check import (  # noqa: F401 (on and work are fixtures)
    CRAWLED,
    ITEMS,
    after_a_run,
    decides,
    on,
    work,
)
from tests.toolkit import Turns, fake_tool, seeded_run

MINE = "read the changelog"
OTHER = "count the lines"


# ---------------------------------------------------------------- the record

async def test_a_record_that_was_made_for_the_owners_items_goes_with_the_last_of_them(conn):
    chat, _ = await seeded_run(conn)
    goal = await goals.add_owner_items(conn, chat.id, [MINE])
    assert (goal.objective, goal.open_items, goal.owner_items) == (goals.OWNER_OBJECTIVE, [MINE], [MINE])
    assert await goals.remove_owner_item(conn, chat.id, MINE) is None and await goals.get(conn, chat.id) is None


async def test_a_record_that_was_made_for_the_owner_stays_while_it_holds_anything(conn):
    chat, _ = await seeded_run(conn)
    await goals.add_owner_items(conn, chat.id, [MINE, OTHER])
    await goals.put(conn, chat.id, goals.OWNER_OBJECTIVE, [], [OTHER], [MINE])
    goal = await goals.remove_owner_item(conn, chat.id, OTHER)
    assert goal is not None and (goal.open_items, goal.done_items) == ([], [MINE])       # one of his that is done
    assert await goals.remove_owner_item(conn, chat.id, MINE) is None
    await goals.add_owner_items(conn, chat.id, [MINE])
    await goals.put(conn, chat.id, goals.OWNER_OBJECTIVE, [], [MINE, "one of the agent's"])
    goal = await goals.remove_owner_item(conn, chat.id, MINE)
    assert goal is not None and goal.open_items == ["one of the agent's"]                 # an item the agent added
    await goals.delete(conn, chat.id)
    await goals.add_owner_items(conn, chat.id, [MINE])
    await goals.put(conn, chat.id, goals.OWNER_OBJECTIVE, ["what the agent wrote"], [MINE])
    goal = await goals.remove_owner_item(conn, chat.id, MINE)
    assert goal is not None and (goal.criteria, goal.open_items) == (["what the agent wrote"], [])
    await goals.delete(conn, chat.id)
    await goals.put(conn, chat.id, "The agent's own goal", [], [])
    await goals.add_owner_items(conn, chat.id, [MINE])
    goal = await goals.remove_owner_item(conn, chat.id, MINE)
    assert goal is not None and goal.objective == "The agent's own goal"                  # a record the agent made


async def test_the_owners_items_lead_the_open_list_in_the_order_he_wrote_them(conn, work):  # noqa: F811
    chat, _ = work
    await goals.add_owner_items(conn, chat.id, [OTHER])
    goal = await goals.add_owner_items(conn, chat.id, [MINE, OTHER, "list endpoints"])      # the last was the agent's
    assert goal.owner_items == [OTHER, MINE, "list endpoints"]
    assert goal.open_items == [OTHER, MINE, "list endpoints", "crawl the site", "read the JS bundles"]
    assert (goal.objective, goal.criteria) == ("Map the API", ["every endpoint listed"])


async def test_whatever_list_is_written_the_owners_open_items_are_on_it(conn):
    chat, _ = await seeded_run(conn)
    await goals.add_owner_items(conn, chat.id, [MINE])
    goal = await goals.put(conn, chat.id, "Map the API", [], ["one"])
    assert goal.open_items == [MINE, "one"] and goal.done_items == []


async def test_an_owners_done_item_stays_done_when_the_done_list_is_cut(conn):
    chat, _ = await seeded_run(conn)
    await goals.add_owner_items(conn, chat.id, [MINE])
    goal = await goals.put(conn, chat.id, "Map the API", [], ["one"], [MINE, *(f"old {n}" for n in range(goals.MAX_DONE + 5))])
    assert goal.open_items == ["one"] and goal.done_items[0] == MINE and len(goal.done_items) == goals.MAX_DONE + 1


async def test_clearing_the_record_clears_what_the_owner_wrote(conn):
    chat, _ = await seeded_run(conn)
    await goals.add_owner_items(conn, chat.id, [MINE])
    await goals.delete(conn, chat.id)
    goal = await goals.put(conn, chat.id, "Map the API", [], ["one"])
    assert goal.open_items == ["one"] and goal.owner_items == []


# ---------------------------------------------------------------- the agent and the owner's items

async def test_the_agent_is_told_which_open_items_the_owner_wrote(conn, work):  # noqa: F811
    chat, focus = work
    block, _ = await focus(action="get")
    assert "owner" not in block.content
    await goals.add_owner_items(conn, chat.id, [MINE])
    block, _ = await focus(action="get")
    assert f"- {MINE}" in block.content and f'only the owner takes one off: "{MINE}"' in block.content


async def test_an_agent_does_not_take_an_owners_item_off_with_a_new_list(conn, work, monkeypatch):  # noqa: F811
    chat, focus = work
    await goals.add_owner_items(conn, chat.id, [MINE])
    decides(monkeypatch, {"items": [{"n": n, "verdict": "not_shown"} for n in (1, 2, 3)]})
    block, goal = await focus(action="update", open_items=["list endpoints"])
    assert goal.open_items == [MINE, "list endpoints"] and goal.done_items == [] and MINE not in goal.marks
    said = block.content.split("Objective:")[0]
    kept, taken = said.split("Taken off the list without a done mark:")
    assert "Kept on the list" in kept and f"- {MINE}" in kept and "said to be done" not in said
    assert MINE not in taken and "- crawl the site" in taken and "- read the JS bundles" in taken


async def test_an_owners_item_that_a_new_list_leaves_out_is_done_when_a_tool_result_shows_it(conn, work, monkeypatch):  # noqa: F811
    chat, focus = work
    await goals.add_owner_items(conn, chat.id, [MINE])
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}]})
    block, goal = await focus(action="update", open_items=list(ITEMS))
    assert goal.open_items == ITEMS and goal.done_items == [MINE] and goal.marks[MINE].verdict == "shown"
    assert "Kept on the list" not in block.content


async def test_an_owners_item_that_was_said_done_and_is_not_shown_is_told_as_that(conn, work, monkeypatch):  # noqa: F811
    chat, focus = work
    await goals.add_owner_items(conn, chat.id, [MINE])
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "not_shown"}]})
    block, goal = await focus(action="update", done=[MINE], open_items=list(ITEMS))
    assert goal.open_items == [MINE, *ITEMS] and goal.marks[MINE].verdict == "not_shown"
    assert "Still open: said to be done" in block.content and "Kept on the list" not in block.content


async def test_a_new_goal_from_the_agent_keeps_what_the_owner_wrote(conn, work, monkeypatch):  # noqa: F811
    chat, focus = work
    await goals.add_owner_items(conn, chat.id, [MINE, OTHER])
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}, {"n": 2, "verdict": "not_shown"},
                                    {"n": 3, "verdict": "shown", "evidence": "Crawl finished"}]})
    block, goal = await focus(action="update", done=[MINE, "crawl the site", "list endpoints"])
    assert goal.done_items == [MINE, "list endpoints"] and goal.marks["crawl the site"].verdict == "not_shown"
    assert "Kept on the list" not in block.content                    # no new list was given: nothing was left out
    block, goal = await focus(action="set", objective="Something else", open_items=["one", "crawl the site"])
    assert f'only the owner takes one off: "{OTHER}"' in block.content and f'"{MINE}"' not in block.content   # his open ones
    assert (goal.objective, goal.criteria, goal.owner_items) == ("Something else", [], [MINE, OTHER])
    assert goal.open_items == [OTHER, "one", "crawl the site"] and goal.done_items == [MINE]
    assert list(goal.marks) == [MINE] and goal.marks[MINE].verdict == "shown"      # nothing of the agent's old goal stays


async def test_the_owner_takes_an_item_of_his_off_and_it_leaves_whole(conn, work, monkeypatch):  # noqa: F811
    chat, focus = work
    await goals.add_owner_items(conn, chat.id, [MINE, OTHER])
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}]})
    await focus(action="update", done=[MINE])
    goal = await goals.remove_owner_item(conn, chat.id, MINE)                    # one that is done
    assert goal.done_items == [] and MINE not in goal.marks and goal.owner_items == [OTHER]
    goal = await goals.remove_owner_item(conn, chat.id, OTHER)                   # one that is open
    assert goal.open_items == ITEMS and goal.owner_items == [] and goal.objective == "Map the API"


async def test_items_past_the_number_read_at_once_stay_open_and_unchecked(conn, work, monkeypatch):  # noqa: F811
    chat, _ = work
    many = [f"item {n}" for n in range(goal_check.MAX_ITEMS + 2)]
    await goals.put(conn, chat.id, "Map the API", [], many)
    asked = decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}]})
    goal, marks, _, _ = await goal_check.settle(conn, await goals.get(conn, chat.id), many)
    assert goal.done_items == ["item 0"] and goal.open_items == many[1:]
    assert marks[many[-1]].verdict == "unchecked" and str(goal_check.MAX_ITEMS) in marks[many[-1]].evidence
    assert f"{goal_check.MAX_ITEMS}. {many[goal_check.MAX_ITEMS - 1]}" in asked[0]["user"] and many[-1] not in asked[0]["user"]


async def test_when_the_answer_says_complete_the_owners_open_items_are_named_under_it(conn, setup, on, monkeypatch):  # noqa: F811
    after_a_run(monkeypatch, True, [{"n": 1, "verdict": "not_shown"}])
    if registry.get("t_crawl") is None:
        registry.register(fake_tool("t_crawl", text=CRAWLED)[0])
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_crawl", {})],
                           [TextDelta("Everything is finished, as the output shows. The work is complete now.")]))
    chat, user = await setup()
    await goals.add_owner_items(conn, chat.id, [MINE])          # the owner's items alone: the agent set no goal
    run, msg = await go(conn, chat, user)
    goal = await goals.get(conn, chat.id)
    assert run.status == "done" and goal.open_items == [MINE] and goal.marks[MINE].verdict == "not_shown"
    notice = next(b for b in msg.blocks if b.type == "notice")
    assert notice.kind == "goal" and MINE in notice.text


# ---------------------------------------------------------------- the API

async def test_the_owner_writes_items_and_takes_them_off_over_the_api(client):  # noqa: F811
    chat = await new_chat(client)
    url = f"/api/chats/{chat['id']}/goal"
    r = await client.post(f"{url}/items", json={"items": ["  read the   changelog ", OTHER, MINE, "  "]})
    assert r.status_code == 200, r.text
    goal = r.json()
    assert goal["objective"] == goals.OWNER_OBJECTIVE and goal["open_items"] == goal["owner_items"] == [MINE, OTHER]
    assert (await client.get(url)).json()["owner_items"] == [MINE, OTHER]
    r = await client.post(f"{url}/items/remove", json={"item": OTHER})
    assert r.status_code == 200 and r.json()["open_items"] == [MINE]
    r = await client.post(f"{url}/items/remove", json={"item": MINE})
    assert r.status_code == 200 and r.json() is None and (await client.get(url)).json() is None


async def test_what_the_api_does_not_take(client):  # noqa: F811
    chat = await new_chat(client)
    url = f"/api/chats/{chat['id']}/goal/items"
    assert (await client.post("/api/chats/nope/goal/items", json={"items": [MINE]})).status_code == 404
    assert (await client.post(f"{url}/remove", json={"item": MINE})).status_code == 404  # no record, nothing of his
    r = await client.post(url, json={"items": ["  ", ""]})
    assert r.status_code == 400 and "at least one item" in r.text
    r = await client.post(url, json={"items": ["x" * (goals.MAX_ITEM + 1)]})
    assert r.status_code == 400 and str(goals.MAX_ITEM) in r.text
    assert (await client.get(f"/api/chats/{chat['id']}/goal")).json() is None           # nothing was written
    most = ["y" * goals.MAX_ITEM, *(f"item {n}" for n in range(goals.MAX_OWNER_ITEMS - 2))]
    assert (await client.post(url, json={"items": most})).status_code == 200            # an item of the full length is taken
    assert (await client.post(url, json={"items": most[:3]})).status_code == 200         # written already: nothing is added
    r = await client.post(url, json={"items": ["the last", " the  last ", "item 0"]})    # one new item, said twice
    assert r.status_code == 200 and r.json()["owner_items"] == [*most, "the last"]
    r = await client.post(url, json={"items": ["one more"]})
    assert r.status_code == 400 and str(goals.MAX_OWNER_ITEMS) in r.text
    from hunterai import db
    conn = await db.connect()
    try:
        goal = await goals.get(conn, chat["id"])
        await goals.put(conn, chat["id"], goal.objective, [], [*goal.open_items, "one of the agent's"])
    finally:
        await conn.close()
    assert (await client.post(f"{url}/remove", json={"item": "one of the agent's"})).status_code == 404
    goal = (await client.get(f"/api/chats/{chat['id']}/goal")).json()
    assert goal["owner_items"] == [*most, "the last"] and goal["open_items"][-1] == "one of the agent's"
