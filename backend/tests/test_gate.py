"""The risk gate: advisory, and it can only add friction (plan decision 10, review focus 2)."""
import asyncio

import pytest

from hunterai.providers import registry as providers
from hunterai.providers.base import ProviderError, ToolCall
from hunterai.repo import settings
from hunterai.tools import registry, runner
from tests.test_decision_engine import Json
from tests.toolkit import answer_next, fake_tool, run_call, seeded_run

SAFE = {"risk": {"0": 0.9, "1": 0.1, "2": 0, "3": 0}, "sensitive": 0.05,
        "recommendation": {"allow": 0.9, "confirm": 0.08, "block": 0.02}}
RISKY = {"risk": {"0": 0, "1": 0.05, "2": 0.15, "3": 0.8}, "sensitive": 0.95,
         "recommendation": {"allow": 0.02, "confirm": 0.18, "block": 0.8}}
CMD = ToolCall("c1", "t_exec", {"command": "rm -rf build"})


@pytest.fixture
async def gated(conn, monkeypatch):
    monkeypatch.setattr(runner, "GATE_DEFAULT", True)
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)

    def decider(*replies):
        d = Json("decider", *replies)
        providers.override("decider", d)
        return d
    return type("G", (), {"ran": ran, "decider": staticmethod(decider)})


def kinds(steps):
    return [(s.kind, s.name, s.status) for s in steps]


async def test_under_allow_a_safe_call_runs_and_the_gate_is_a_step(conn, gated):
    d = gated.decider(SAFE)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    out, events, steps = await run_call(conn, chat, run_id, CMD)
    assert not out.block.is_error and gated.ran == [{"command": "rm -rf build"}]
    assert kinds(steps) == [("decision", "risk gate", "done"), ("permission", "t_exec", "done"),
                            ("tool_call", "t_exec", "done")]
    g = steps[0].detail
    assert g["recommendation"] == "allow" and g["risk_score"] < 0.5 and g["tool_call_id"] == "c1"
    sent = d.calls[0]["messages"][0].blocks[0].text
    assert "rm -rf build" in sent and "t_exec" in sent
    assert all(e["type"] != "permission_request" for e in events)


async def test_under_allow_a_risky_call_becomes_an_ask(conn, gated):
    gated.decider(RISKY)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    task = asyncio.create_task(run_call(conn, chat, run_id, CMD))
    req = await answer_next(run_id, "deny")
    out, _, steps = await task
    assert gated.ran == [] and out.block.is_error
    assert req.gate["recommendation"] == "block" and req.gate["risk_score"] > 2
    assert any("risk gate" in r for r in req.reasons)
    perm = next(s for s in steps if s.kind == "permission")
    assert perm.detail["rule"] == "gate" and perm.status == "denied"


async def test_under_ask_the_score_is_shown_and_a_safe_score_does_not_approve(conn, gated):
    gated.decider(SAFE)
    chat, run_id = await seeded_run(conn)                           # pentesting: execute asks
    task = asyncio.create_task(run_call(conn, chat, run_id, CMD))
    req = await answer_next(run_id, "allow_once")
    await task
    assert req.gate["recommendation"] == "allow" and gated.ran == [{"command": "rm -rf build"}]


async def test_under_deny_nothing_runs_and_the_gate_is_not_asked(conn, gated):
    d = gated.decider(SAFE)
    chat, run_id = await seeded_run(conn, mode="plan")              # plan: execute is denied
    out, _, steps = await run_call(conn, chat, run_id, CMD, offered={"t_exec"})
    assert gated.ran == [] and d.calls == [] and kinds(steps) == [("permission", "t_exec", "denied")]


async def test_a_gate_that_cannot_be_reached_keeps_the_decision(conn, gated):
    gated.decider(ProviderError("bad_request", "connection refused"))
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    out, _, steps = await run_call(conn, chat, run_id, CMD)
    assert not out.block.is_error and gated.ran == [{"command": "rm -rf build"}]
    assert kinds(steps)[0] == ("decision", "risk gate", "error") and steps[0].detail["failed"] is True


async def test_an_unusable_gate_reply_becomes_an_ask(conn, gated):
    gated.decider({"risk": {"0": "x"}, "sensitive": 0.1, "recommendation": {"allow": 1}})
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    task = asyncio.create_task(run_call(conn, chat, run_id, CMD))
    req = await answer_next(run_id, "allow_once")
    await task
    assert req.gate["invalid"] is True and req.gate["recommendation"] == "confirm"


async def test_a_reply_that_is_not_json_becomes_an_ask_too(conn, gated):
    gated.decider("sure, go ahead", "still not json")
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    task = asyncio.create_task(run_call(conn, chat, run_id, CMD))
    req = await answer_next(run_id, "deny")
    await task
    assert req.gate["invalid"] is True and req.gate["recommendation"] == "confirm" and gated.ran == []


MIXED = {"risk": {"0": 0, "1": 0.1, "2": 0.2, "3": 0.7}, "sensitive": 0.9,
         "recommendation": {"allow": 0.9, "confirm": 0.05, "block": 0.05}}
UNSURE = {"risk": {"0": 0.9, "1": 0.1, "2": 0, "3": 0}, "sensitive": 0.05,
          "recommendation": {"allow": 0.4, "confirm": 0.35, "block": 0.25}}


@pytest.mark.parametrize("reply", [MIXED, UNSURE])
async def test_allow_with_a_high_risk_score_or_little_confidence_still_asks(conn, gated, reply):
    gated.decider(reply)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    task = asyncio.create_task(run_call(conn, chat, run_id, CMD))
    req = await answer_next(run_id, "deny")
    await task
    assert gated.ran == [] and any("risk gate" in r for r in req.reasons)


async def test_the_gate_sees_both_ends_of_long_arguments_and_a_cut_view_asks(conn, gated):
    d = gated.decider(SAFE)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    command = "echo " + "a" * 20_000 + " ; rm -rf ~/keep"
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "t_exec", {"command": command})))
    req = await answer_next(run_id, "deny")
    await task
    sent = d.calls[0]["messages"][0].blocks[0].text
    assert "rm -rf ~/keep" in sent and "characters omitted" in sent
    assert req.gate["partial"] is True and gated.ran == []     # SAFE, but the gate did not see everything


async def test_the_gate_is_told_the_resolved_paths_and_hosts(conn, gated):
    from hunterai.tools.base import HostUse, PathUse, Prepared
    d = gated.decider(SAFE)
    tool, _ = fake_tool("t_write", risk="write", prepared=Prepared(
        paths=(PathUse("/home/u/.bashrc", "write"),), hosts=(HostUse("prod.example.com", 443),)))
    registry.register(tool)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"write": "allow"}})
    await run_call(conn, chat, run_id, ToolCall("c1", "t_write", {"path": "x"}))
    sent = d.calls[0]["messages"][0].blocks[0].text
    assert "/home/u/.bashrc" in sent and "prod.example.com" in sent


async def test_read_calls_and_the_switch(conn, gated):
    d = gated.decider(RISKY, RISKY)
    read, read_ran = fake_tool("t_read"); registry.register(read)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    await run_call(conn, chat, run_id, ToolCall("c0", "t_read", {}))
    assert d.calls == [] and read_ran == [{}]                       # only execute, write, edit
    await settings.put(conn, "decision_gate", False)
    out, _, steps = await run_call(conn, chat, run_id, CMD)
    assert d.calls == [] and not out.block.is_error and all(s.kind != "decision" for s in steps)


async def test_a_call_the_owner_allowed_for_the_chat_is_not_gated_again(conn, gated):
    d = gated.decider(RISKY, RISKY)
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, CMD))
    req = await answer_next(run_id, "allow_chat")
    await task
    assert req.can_allow_chat and len(d.calls) == 1
    out, events, _ = await run_call(conn, chat, run_id, ToolCall("c2", "t_exec", {"command": "rm -rf build"}))
    assert len(d.calls) == 1 and not out.block.is_error             # the grant stands, no second gate
    assert all(e["type"] != "permission_request" for e in events)


async def test_the_built_in_decision_engine_can_be_switched_off_for_another_one(conn, gated):
    from hunterai.tools import native
    native.register_all()
    d = gated.decider(RISKY, RISKY)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    names = {t.name for t in await runner.offered_tools(conn, chat)}
    assert {"decide_check", "decide_gate", "decide_score"} <= names
    await settings.put(conn, "decision_engine", False)
    names = {t.name for t in await runner.offered_tools(conn, chat)}
    assert not any(n.startswith("decide_") for n in names) and {"read", "web_fetch", "focus"} <= names
    out, _, steps = await run_call(conn, chat, run_id, CMD)                  # and the risk gate, which is that engine, is not asked
    assert d.calls == [] and all(st.kind != "decision" for st in steps)
