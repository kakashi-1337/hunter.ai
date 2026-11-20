"""A done mark on an item of the goal record: only when a tool result of the chat shows the work."""
import pytest

from hunterai import goal_check
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import goals, settings
from hunterai.tools import mind, registry
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, fake_tool, run_call, seeded_run

CRAWLED = "Crawl finished. Found 132 URLs on shop.example."
ITEMS = ["crawl the site", "read the JS bundles", "list endpoints"]


def decides(monkeypatch, *answers, error=None):
    """The decision model answers `answers`, one a call; what it was asked is returned."""
    asked, queue = [], list(answers)

    async def complete_json(conn, system, user, schema, **kw):
        asked.append({"system": system, "user": user, "schema": schema})
        meta = {"provider": "ollama", "model": "judge", "ms": 5, "usage": {}}
        if error:
            return None, {**meta, "error": error}
        return (queue.pop(0) if queue else {}), meta
    monkeypatch.setattr(goal_check.de, "complete_json", complete_json)
    return asked


@pytest.fixture
async def work(conn):
    """A chat with three open items and one tool result. Gives (chat, focus): focus(**args) calls the tool the way
    a model does and returns (its result block, the goal record after it)."""
    for t in mind.tools():
        registry.register(t)
    registry.register(fake_tool("probe", text=CRAWLED)[0])
    chat, run_id = await seeded_run(conn)
    await goals.put(conn, chat.id, "Map the API", ["every endpoint listed"], list(ITEMS))
    await run_call(conn, chat, run_id, ToolCall("p1", "probe", {}))
    n = 0

    async def focus(**args):
        nonlocal n
        n += 1
        out, _, _ = await run_call(conn, chat, run_id, ToolCall(f"f{n}", "focus", args))
        return out.block, await goals.get(conn, chat.id)
    return chat, focus


async def test_an_item_is_marked_done_when_a_tool_result_shows_it(work, monkeypatch):
    _, focus = work
    asked = decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}]})
    block, goal = await focus(action="update", done=["crawl the site"])
    assert goal.open_items == ["read the JS bundles", "list endpoints"] and goal.done_items == ["crawl the site"]
    mark = goal.marks["crawl the site"]
    assert (mark.verdict, mark.evidence, mark.tool, mark.model) == ("shown", "Found 132 URLs", "probe", "ollama/judge")
    assert not block.is_error and '- crawl the site: "Found 132 URLs" (probe)' in block.content
    # What the decision model was given: the item by its number, and the tool result as data.
    assert "1. crawl the site" in asked[0]["user"] and CRAWLED in asked[0]["user"] and "untrusted" in asked[0]["system"]


async def test_an_item_no_tool_result_shows_stays_open(work, monkeypatch):
    _, focus = work
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "not_shown"}]})
    block, goal = await focus(action="update", done=["read the JS bundles"])
    assert goal.open_items == ITEMS and goal.done_items == []
    assert goal.marks["read the JS bundles"].verdict == "not_shown"
    assert not block.is_error and "Still open" in block.content and "- read the JS bundles" in block.content
    assert "open_items" in block.content            # the way out for an item that is no longer needed is said


async def test_words_that_stand_in_no_tool_result_mark_nothing(work, monkeypatch):
    _, focus = work
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "All 14 bundles were read and parsed"}]})
    _, goal = await focus(action="update", done=["read the JS bundles"])
    assert goal.done_items == [] and "read the JS bundles" in goal.open_items
    mark = goal.marks["read the JS bundles"]
    assert mark.verdict == "not_shown" and "in no tool result" in mark.evidence and mark.tool == ""


async def test_the_quoted_words_are_found_whatever_their_spacing_case_and_quote_marks(work, monkeypatch):
    _, focus = work
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": '"crawl finished. ...   found 132 urls"'}]})
    _, goal = await focus(action="update", done=["crawl the site"])
    assert goal.done_items == ["crawl the site"] and goal.marks["crawl the site"].tool == "probe"


async def test_too_few_words_are_no_evidence(work, monkeypatch):
    _, focus = work
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "on"}]})      # "on" stands in nearly every text
    _, goal = await focus(action="update", done=["crawl the site"])
    assert goal.done_items == [] and goal.marks["crawl the site"].verdict == "not_shown"


async def test_a_result_that_says_the_opposite_keeps_the_item_open_and_is_quoted(conn, work, monkeypatch):
    chat, focus = work
    registry.register(fake_tool("fetch", text="curl: (7) Failed to connect to shop.example port 443")[0])
    run_id = (await conn.execute("select id from runs where chat_id=?", (chat.id,)))
    run_id = (await run_id.fetchone())["id"]
    await run_call(conn, chat, run_id, ToolCall("p2", "fetch", {}))
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "contradicted", "evidence": "Failed to connect to shop.example"}]})
    block, goal = await focus(action="update", done=["list endpoints"])
    mark = goal.marks["list endpoints"]
    assert "list endpoints" in goal.open_items and (mark.verdict, mark.tool) == ("contradicted", "fetch")
    assert 'says the opposite: "Failed to connect to shop.example" (fetch)' in block.content


async def test_without_a_usable_verdict_nothing_is_marked(work, monkeypatch):
    _, focus = work
    decides(monkeypatch, error="the decision model did not return a JSON object")
    block, goal = await focus(action="update", done=["crawl the site"])
    assert goal.done_items == [] and goal.open_items == ITEMS
    assert goal.marks["crawl the site"].verdict == "unchecked"
    assert not block.is_error and "could not be checked" in block.content and "did not return a JSON object" in block.content
    # An item the decision model says nothing about is not checked either.
    decides(monkeypatch, {"items": [{"n": 2, "verdict": "shown", "evidence": "Found 132 URLs"}]})
    _, goal = await focus(action="update", done=["crawl the site"])
    assert goal.done_items == [] and goal.marks["crawl the site"].verdict == "unchecked"


async def test_a_reply_that_is_a_little_off_its_shape_is_still_read(work, monkeypatch):
    """A small model without enforced output writes the number as text, the verdict with a space, the item by name."""
    _, focus = work
    decides(monkeypatch, {"items": [{"n": "1", "verdict": "Shown", "quote": "Found 132 URLs"},
                                    {"item": "read the JS bundles", "verdict": "not shown"},
                                    {"n": 7, "verdict": "shown", "evidence": "Found 132 URLs"},        # no such item
                                    {"n": 3, "verdict": "perhaps"}, "not an object"]})
    _, goal = await focus(action="update", done=list(ITEMS))
    assert goal.done_items == ["crawl the site"] and goal.open_items == ["read the JS bundles", "list endpoints"]
    assert goal.marks["read the JS bundles"].verdict == "not_shown" and goal.marks["list endpoints"].verdict == "unchecked"


async def test_in_a_chat_without_a_tool_result_nothing_can_be_shown_and_no_model_is_asked(conn, monkeypatch):
    for t in mind.tools():
        registry.register(t)
    chat, run_id = await seeded_run(conn)
    await goals.put(conn, chat.id, "Map the API", [], list(ITEMS))
    asked = decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}]})
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("f1", "focus", {"action": "update", "done": ["crawl the site"]}))
    goal = await goals.get(conn, chat.id)
    assert asked == [] and goal.done_items == [] and goal.marks["crawl the site"].verdict == "not_shown"
    assert "Still open" in out.block.content


async def test_the_evidence_is_what_tools_brought_not_what_the_agent_wrote_down(conn, work):
    chat, focus = work
    await focus(action="get")                               # the record itself would "show" every item
    other, other_run = await seeded_run(conn)
    await run_call(conn, other, other_run, ToolCall("o1", "probe", {}))
    again, _ = await seeded_run(conn)                       # a second run of another chat changes nothing here
    pieces = await goal_check.evidence(conn, chat.id)
    assert [(p.tool, p.is_error) for p in pieces] == [("probe", False)] and CRAWLED in pieces[0].text
    assert goal_check.NOT_EVIDENCE >= {"focus", "critique", "delegate", "swarm", "skill"}
    assert await goal_check.evidence(conn, again.id) == []


async def test_an_item_that_leaves_through_a_new_list_is_done_only_when_shown(work, monkeypatch):
    _, focus = work
    asked = decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"},
                                            {"n": 2, "verdict": "not_shown"}]})
    block, goal = await focus(action="update", open_items=["list endpoints"])
    assert "1. crawl the site\n2. read the JS bundles" in asked[0]["user"]
    assert goal.open_items == ["list endpoints"] and goal.done_items == ["crawl the site"]
    assert "read the JS bundles" not in goal.marks            # taken off, not done: nothing is kept of it
    assert "without a done mark" in block.content and "- read the JS bundles" in block.content


async def test_an_item_said_done_stays_open_until_shown_even_when_the_new_list_leaves_it_out(work, monkeypatch):
    _, focus = work
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "not_shown"}]})
    _, goal = await focus(action="update", done=["read the JS bundles"], open_items=["crawl the site", "list endpoints"])
    assert goal.open_items == ["crawl the site", "list endpoints", "read the JS bundles"] and goal.done_items == []


async def test_a_name_that_is_not_on_the_open_list_is_said_back_and_costs_no_model_call(work, monkeypatch):
    _, focus = work
    asked = decides(monkeypatch)
    block, goal = await focus(action="update", done=["crawl site"])
    assert asked == [] and goal.open_items == ITEMS and goal.done_items == []
    assert "Not on the open list" in block.content and '"crawl site"' in block.content


async def test_an_item_that_comes_back_is_open_again_and_a_new_goal_starts_clean(work, monkeypatch):
    _, focus = work
    decides(monkeypatch, {"items": [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}]})
    _, goal = await focus(action="update", done=["crawl the site"])
    assert goal.done_items == ["crawl the site"]
    _, goal = await focus(action="update", add_open=["crawl the site"])
    assert "crawl the site" in goal.open_items and goal.done_items == [] and "crawl the site" not in goal.marks
    _, goal = await focus(action="set", objective="Something else", open_items=["one"])
    assert (goal.objective, goal.open_items, goal.done_items, goal.marks) == ("Something else", ["one"], [], {})


async def test_the_goal_route_gives_the_marks(client):  # noqa: F811
    chat = await new_chat(client)
    from hunterai import db
    conn = await db.connect()
    try:
        await goals.put(conn, chat["id"], "Find an IDOR", ["one"], ["two"], ["zero"])
        await goals.mark(conn, chat["id"], "zero", goals.Mark(verdict="shown", evidence="uid=1000", tool="execute", model="ollama/judge"))
        await goals.mark(conn, chat["id"], "two", goals.Mark(verdict="not_shown"))
    finally:
        await conn.close()
    goal = (await client.get(f"/api/chats/{chat['id']}/goal")).json()
    assert goal["marks"]["zero"]["verdict"] == "shown" and goal["marks"]["zero"]["evidence"] == "uid=1000"
    assert goal["marks"]["zero"]["tool"] == "execute" and goal["marks"]["zero"]["at"]
    assert goal["marks"]["two"]["verdict"] == "not_shown"
    await client.delete(f"/api/chats/{chat['id']}/goal")
    conn = await db.connect()
    try:
        left = await (await conn.execute("select count(*) n from goal_marks where chat_id=?", (chat["id"],))).fetchone()
    finally:
        await conn.close()
    assert left["n"] == 0


# ---------------------------------------------------------------- after a run that says it is complete

@pytest.fixture
def on(monkeypatch):
    monkeypatch.delenv("HUNTERAI_ANSWER_CHECK")


def after_a_run(monkeypatch, complete, items=()):
    """The decision model as the checks after a run ask it. Returns what it was asked, as "answer", "complete"
    or "items"."""
    asked = []

    async def complete_json(conn, system, user, schema, **kw):
        meta = {"provider": "ollama", "model": "judge", "ms": 5, "usage": {}}
        if "claims" in schema["properties"]:
            asked.append("answer")
            return {"claims": []}, meta
        if "complete" in schema["properties"]:
            asked.append("complete")
            return {"complete": complete}, meta
        asked.append("items")
        return {"items": list(items)}, meta
    monkeypatch.setattr(goal_check.de, "complete_json", complete_json)
    return asked


async def a_run(conn, setup, answer, open_items=("crawl the site", "read the JS bundles")):  # noqa: F811
    if registry.get("t_crawl") is None:
        registry.register(fake_tool("t_crawl", text=CRAWLED)[0])
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_crawl", {})], [TextDelta(answer)]))
    chat, user = await setup()
    if open_items is not None:
        await goals.put(conn, chat.id, "Map the API", [], list(open_items))
    run, msg = await go(conn, chat, user)
    return chat, run, msg


async def test_when_the_answer_says_complete_the_open_items_are_checked_and_marked(conn, setup, on, monkeypatch):  # noqa: F811
    asked = after_a_run(monkeypatch, True, [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"},
                                            {"n": 2, "verdict": "not_shown"}])
    chat, run, msg = await a_run(conn, setup, "All done: the site is crawled and the bundles are read. The work is complete.")
    goal = await goals.get(conn, chat.id)
    assert run.status == "done" and asked == ["answer", "complete", "items"]
    assert goal.done_items == ["crawl the site"] and goal.open_items == ["read the JS bundles"]
    assert goal.marks["crawl the site"].tool == "t_crawl" and goal.marks["read the JS bundles"].verdict == "not_shown"
    notice = next(b for b in msg.blocks if b.type == "notice")
    assert notice.kind == "goal" and "read the JS bundles" in notice.text and "crawl the site" not in notice.text
    step = next(s for s in run.steps if s.name == "goal check")
    assert (step.kind, step.status, step.detail["complete"], step.detail["shown"]) == ("decision", "done", True, 1)


async def test_when_every_open_item_is_shown_nothing_is_put_under_the_answer(conn, setup, on, monkeypatch):  # noqa: F811
    after_a_run(monkeypatch, True, [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}])
    chat, _, msg = await a_run(conn, setup, "The site is crawled, as the output shows. The work is complete now.",
                               open_items=("crawl the site",))
    goal = await goals.get(conn, chat.id)
    assert goal.open_items == [] and goal.done_items == ["crawl the site"]
    assert all(b.type != "notice" for b in msg.blocks)


async def test_an_answer_that_does_not_say_complete_marks_nothing(conn, setup, on, monkeypatch):  # noqa: F811
    asked = after_a_run(monkeypatch, False, [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}])
    chat, run, msg = await a_run(conn, setup, "I crawled the site and found 132 URLs. Next I would read the bundles: shall I go on?")
    goal = await goals.get(conn, chat.id)
    assert asked == ["answer", "complete"]                   # the tool results are not read again for nothing
    assert goal.done_items == [] and goal.marks == {} and all(b.type != "notice" for b in msg.blocks)
    step = next(s for s in run.steps if s.name == "goal check")
    assert step.status == "done" and step.detail["complete"] is False


async def test_without_a_goal_record_or_an_open_item_nothing_more_is_asked(conn, setup, on, monkeypatch):  # noqa: F811
    asked = after_a_run(monkeypatch, True)
    _, run, _ = await a_run(conn, setup, "The site is crawled, as the output shows. The work is complete now.", open_items=None)
    assert asked == ["answer"] and all(s.name != "goal check" for s in run.steps)
    asked.clear()
    _, run, _ = await a_run(conn, setup, "The site is crawled, as the output shows. The work is complete now.", open_items=())
    assert asked == ["answer"] and all(s.name != "goal check" for s in run.steps)


async def test_the_goal_check_is_switched_off_with_the_answer_check_and_never_fails_a_run(conn, setup, on, monkeypatch):  # noqa: F811
    asked = after_a_run(monkeypatch, True, [{"n": 1, "verdict": "shown", "evidence": "Found 132 URLs"}])
    await settings.put(conn, "answer_check", False)
    chat, run, _ = await a_run(conn, setup, "The site is crawled, as the output shows. The work is complete now.")
    assert asked == [] and (await goals.get(conn, chat.id)).done_items == []
    await settings.put(conn, "answer_check", True)

    async def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(goal_check.de, "complete_json", broken)
    chat, run, msg = await a_run(conn, setup, "The site is crawled, as the output shows. The work is complete now.")
    assert run.status == "done" and msg.status == "complete" and (await goals.get(conn, chat.id)).done_items == []
    step = next(s for s in run.steps if s.name == "goal check")
    assert step.status == "error"
