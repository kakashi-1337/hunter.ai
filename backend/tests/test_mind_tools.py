"""Goal record + focus, critique, and findings with the triage preset."""
import json

import pytest

from hunterai import db, modes
from hunterai.providers import registry
from hunterai.providers.base import ToolCall
from hunterai.repo import findings, goals
from hunterai.tools import mind, registry as tools
from tests.test_decision_engine import Json
from tests.toolkit import run_call, seeded_run


@pytest.fixture
def mind_registered():
    for t in mind.tools():
        tools.register(t)


async def call(conn, chat, run_id, name, reply=None, **args):
    if reply is not None:
        registry.override("abliteration", Json("abliteration", *reply))
    out, _, steps = await run_call(conn, chat, run_id, ToolCall("c", name, args))
    return out.block, steps


async def test_migration_adds_the_new_tables_and_columns(conn):
    assert db.VERSION >= 3
    names = {r["name"] for r in await (await conn.execute("select name from sqlite_master")).fetchall()}
    assert {"goals", "subruns"} <= names
    cols = {r["name"] for r in await (await conn.execute("pragma table_info(steps)")).fetchall()}
    assert "parent_step_id" in cols
    cols = {r["name"] for r in await (await conn.execute("pragma table_info(findings)")).fetchall()}
    assert "triage_json" in cols


async def test_focus_sets_reads_updates_and_survives(conn, mind_registered):
    chat, run_id = await seeded_run(conn)
    block, _ = await call(conn, chat, run_id, "focus", action="get")
    assert "No goal record yet" in block.content and not block.is_error
    block, _ = await call(conn, chat, run_id, "focus", action="set", objective="Find an IDOR in the invoices API",
                          success_criteria=["Read another user's invoice", "Reproducible request"],
                          open_items=["Map the endpoints"])
    assert "Find an IDOR in the invoices API" in block.content
    g = await goals.get(conn, chat.id)
    assert (g.objective, g.criteria, g.open_items) == (
        "Find an IDOR in the invoices API", ["Read another user's invoice", "Reproducible request"], ["Map the endpoints"])
    block, _ = await call(conn, chat, run_id, "focus", action="update", add_open=["Try id 1338", "Map the endpoints"],
                          done=["Map the endpoints"])
    g = await goals.get(conn, chat.id)
    # "Map the endpoints" was only said to be done: no tool result of this chat shows it, so it stays open.
    assert g.open_items == ["Map the endpoints", "Try id 1338"] and g.objective.startswith("Find an IDOR")
    block, _ = await call(conn, chat, run_id, "focus", action="set")
    assert block.is_error and "objective" in block.content
    block, _ = await call(conn, chat, run_id, "focus", action="update", add_open=["x"])
    assert not block.is_error
    other, other_run = await seeded_run(conn)
    block, _ = await call(conn, other, other_run, "focus", action="update", add_open=["x"])
    assert block.is_error and "set" in block.content            # nothing to update in a chat with no record


async def test_focus_check_reports_drift_from_the_decision_engine(conn, mind_registered):
    chat, run_id = await seeded_run(conn)
    block, _ = await call(conn, chat, run_id, "focus", action="check", next_step="scan ports")
    assert block.is_error                                        # no goal record yet
    await goals.put(conn, chat.id, "Find an IDOR in the invoices API", [], [])
    block, _ = await call(conn, chat, run_id, "focus", [{"serves": 0.1}], action="check",
                          next_step="Rewrite the README of an unrelated project")
    got = json.loads(block.content)
    assert got["drift_probability"] == 0.9 and got["verdict"] == "drifting"
    block, _ = await call(conn, chat, run_id, "focus", [{"serves": 0.95}], action="check", next_step="Request invoice 1338")
    assert json.loads(block.content)["verdict"] == "on track"
    block, _ = await call(conn, chat, run_id, "focus", ["garbage"], action="check", next_step="x")
    got = json.loads(block.content)
    assert got["verdict"] == "unclear" and got["invalid"] is True      # never "on track" on a bad reply


async def test_goal_record_is_in_the_system_prompt(conn):
    chat, _ = await seeded_run(conn)
    await goals.put(conn, chat.id, "Find an IDOR", ["Read another invoice"], ["Try id 1338"])
    s = modes.build_system("pentesting", None, ["focus"], None, goal=await goals.get(conn, chat.id))
    assert "Goal record" in s and "Objective: Find an IDOR" in s
    assert "- Read another invoice" in s and "- Try id 1338" in s
    assert "Goal record" not in modes.build_system("pentesting", None, ["focus"], None)


async def test_critique_returns_a_labelled_structured_analysis(conn, mind_registered):
    chat, run_id = await seeded_run(conn)
    reply = {"assumptions": ["The session token belongs to user A"], "alternatives": ["The invoice is shared"],
             "would_disprove": ["A 403 for a third user's invoice"],
             "evidence_check": [{"point": "200 for id 1338", "supported": "yes", "why": "the response is quoted"}],
             "weakest_point": "Only one id was tried", "verdict": "Plausible, needs a second id"}
    block, _ = await call(conn, chat, run_id, "critique", [reply], claim="This endpoint has an IDOR",
                          evidence="GET /invoices/1338 -> 200")
    text = block.content
    assert text.startswith("Model analysis, not a verified fact.") and "Only one id was tried" in text
    assert "The invoice is shared" in text and "[yes] 200 for id 1338" in text and not block.is_error
    p = registry.get("abliteration")
    sent = p.calls[0]["messages"][0].blocks[0].text
    assert "\\u003cb\\u003e" in (await _sent_with(conn, chat, run_id, "a <b> claim"))   # wrapped as untrusted data
    assert "This endpoint has an IDOR" in sent and "GET /invoices/1338" in sent
    block, _ = await call(conn, chat, run_id, "critique", ["not json"], claim="x")
    assert block.is_error and "could not" in block.content


async def _sent_with(conn, chat, run_id, claim):
    p = Json("abliteration", {})
    registry.override("abliteration", p)
    await run_call(conn, chat, run_id, ToolCall("c", "critique", {"claim": claim}))
    return p.calls[0]["messages"][0].blocks[0].text


TRIAGE = {"severity": {"0": 0, "1": 0, "2": 0.1, "3": 0.8, "4": 0.1}, "false_positive": 0.1, "in_scope": 0.9,
          "duplicate": 0.05}


async def test_findings_are_triaged_stored_listed_and_updated(conn, mind_registered):
    chat, run_id = await seeded_run(conn, scope={"in": ["api.acme.test"], "out": []})
    block, steps = await call(conn, chat, run_id, "finding_add", [TRIAGE], title="IDOR on invoices",
                              evidence="GET api.acme.test/invoices/1338 -> 200 with another user's data")
    got = json.loads(block.content)
    assert got["severity"] == "high" and got["status"] == "open" and got["triage"]["false_positive"] == 0.1
    assert got["triage"]["in_scope"] == 0.9 and got["triage"]["severity_confidence"] > 0
    stored = (await findings.list_for_target(conn, chat.target_id))[0]
    assert (stored.title, stored.severity, stored.chat_id, stored.step_id) == ("IDOR on invoices", "high", chat.id, steps[-1].id)
    sent = registry.get("abliteration").calls[0]["messages"][0].blocks[0].text
    assert "api.acme.test" in sent and "IDOR on invoices" in sent
    block, _ = await call(conn, chat, run_id, "finding_add", [{**TRIAGE, "duplicate": 0.9}], title="IDOR again",
                          evidence="same request", severity="critical")
    got2 = json.loads(block.content)
    assert got2["severity"] == "critical" and got2["triage"]["duplicate"] == 0.9      # the model's own severity wins
    assert "IDOR on invoices" in registry.get("abliteration").calls[0]["messages"][0].blocks[0].text
    block, _ = await call(conn, chat, run_id, "finding_update", id=got["id"], status="confirmed", note="reproduced twice")
    assert json.loads(block.content)["status"] == "confirmed"
    assert "reproduced twice" in (await findings.get(conn, got["id"])).evidence
    block, _ = await call(conn, chat, run_id, "finding_list")
    assert [f["title"] for f in json.loads(block.content)["findings"]] == ["IDOR on invoices", "IDOR again"]
    block, _ = await call(conn, chat, run_id, "finding_list", status="confirmed")
    assert len(json.loads(block.content)["findings"]) == 1
    block, _ = await call(conn, chat, run_id, "finding_update", id="nope", status="fixed")
    assert block.is_error


async def test_a_failed_triage_still_stores_the_finding_as_untriaged(conn, mind_registered):
    chat, run_id = await seeded_run(conn, scope={"in": [], "out": []})
    block, _ = await call(conn, chat, run_id, "finding_add", ["garbage"], title="Open redirect", evidence="/go?u=//evil")
    got = json.loads(block.content)
    assert got["severity"] == "info" and got["triage"]["invalid"] is True and not block.is_error
    assert len(await findings.list_for_target(conn, chat.target_id)) == 1


async def test_findings_need_a_target(conn, mind_registered):
    chat, run_id = await seeded_run(conn)                        # the "No target" bucket
    block, _ = await call(conn, chat, run_id, "finding_add", [TRIAGE], title="x", evidence="y")
    assert block.is_error and "target" in block.content
