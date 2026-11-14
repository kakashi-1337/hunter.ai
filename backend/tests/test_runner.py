import asyncio
import dataclasses
import json

import pytest

from hunterai import config, permissions
from hunterai.providers.base import ToolCall
from hunterai.repo import chats, grants, runs
from hunterai.tools import registry, runner
from hunterai.tools.base import HostUse, PathUse, Prepared, ToolError
from hunterai.types import ToolResultBlock
from tests import toolkit
from tests.toolkit import answer_next, fake_tool, seeded_run

ECHO = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
KEY = "ak_secret_value_123"

go = toolkit.run_call

async def test_allowed_call_runs_and_records_a_permission_and_a_tool_step(conn):
    tool, ran = fake_tool("t_echo", schema=ECHO); registry.register(tool)
    chat, run_id = await seeded_run(conn)
    out, events, steps = await go(conn, chat, run_id, ToolCall("c1", "t_echo", {"text": "hi"}))
    assert out == runner.Outcome(ToolResultBlock(tool_call_id="c1", content="ok", is_error=False), False) and ran == [{"text": "hi"}]
    assert [(s.kind, s.name, s.status) for s in steps] == [("permission", "t_echo", "done"), ("tool_call", "t_echo", "done")]
    assert steps[0].detail["rule"] == "preset" and steps[1].detail["result_preview"] == "ok" and steps[1].ended_at
    assert [e["type"] for e in events] == ["step_started", "step_finished", "step_started", "tool_started", "step_finished", "tool_finished"]

@pytest.mark.parametrize("tc,needle", [
    (ToolCall("c1", "nope", {}), 'Unknown tool "nope". Available tools: t_echo.'),
    (ToolCall("c1", "t_echo", {}, error="arguments are not valid JSON: x"), "Invalid call to t_echo: arguments are not valid JSON"),
    (ToolCall("c1", "t_echo", {"text": 5}), "Invalid arguments for t_echo: $.text:"),
    (ToolCall("c1", "t_hidden", {}), 'Unknown tool "t_hidden"'),
])
async def test_invalid_calls_return_an_error_and_run_nothing(conn, tc, needle):      # Review Focus 3
    tool, ran = fake_tool("t_echo", schema=ECHO); registry.register(tool)
    hidden, hidden_ran = fake_tool("t_hidden"); registry.register(dataclasses.replace(hidden, expose=False))
    chat, run_id = await seeded_run(conn)
    out, events, steps = await go(conn, chat, run_id, tc)
    assert out.invalid and out.block.is_error and needle in out.block.content and ran == [] and hidden_ran == []
    assert [(s.kind, s.status) for s in steps] == [("tool_call", "error")] and events[-1]["type"] == "tool_finished"

async def test_allow_for_chat_covers_the_same_target_only(conn):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command"); registry.register(tool)
    chat, run_id = await seeded_run(conn)
    first = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "t_exec", {"command": "id"})))
    req = await answer_next(run_id, "allow_chat")
    assert req.arguments == {"command": "id"} and req.grant_target == "id" and req.can_allow_chat
    await first
    await go(conn, chat, run_id, ToolCall("c2", "t_exec", {"command": "id"}))             # same target: no ask
    other = asyncio.create_task(go(conn, chat, run_id, ToolCall("c3", "t_exec", {"command": "rm -rf ~"})))
    req = await answer_next(run_id, "deny", note="no")                                    # a different command asks again
    out, _, steps = await other
    assert req.arguments == {"command": "rm -rf ~"} and ran == [{"command": "id"}, {"command": "id"}]
    assert out.block.content == "The owner denied this call. Owner's note: no" and out.block.is_error and not out.invalid
    assert [s.status for s in steps if s.kind == "permission"] == ["done", "done", "denied"]

async def test_long_output_keeps_head_and_tail_and_names_the_full_file(conn):
    body = "A" * 25_000 + "MIDDLE" + "Z" * 25_000
    tool, _ = fake_tool("t_big", text=body); registry.register(tool)
    chat, run_id = await seeded_run(conn)
    out, _, steps = await go(conn, chat, run_id, ToolCall("c1", "t_big", {}))
    text, path = out.block.content, config.output_dir() / run_id / f"{steps[-1].id}.txt"
    assert text.startswith("A" * 22_000 + "\n[... ") and text.endswith("Z" * 8_000) and "MIDDLE" not in text
    assert f"{len(body) - 30_000} of {len(body)} characters omitted" in text and f'read(path="{path}", chunk=N)' in text
    assert path.read_text() == body and steps[-1].detail["truncated"] is True and steps[-1].detail["result_chars"] == len(body)

async def test_hostile_call_id_cannot_choose_the_output_path(conn):
    tool, _ = fake_tool("t_big", text="A" * 40_000); registry.register(tool)
    chat, run_id = await seeded_run(conn)
    out, _, steps = await go(conn, chat, run_id, ToolCall("../../evil", "t_big", {}))
    saved = [f for f in config.output_dir().rglob("*") if f.is_file()]
    assert saved == [config.output_dir() / run_id / f"{steps[-1].id}.txt"] and not list(config.home().glob("evil*"))
    assert str(saved[0]) in out.block.content and saved[0].stat().st_mode & 0o777 == 0o600

async def test_key_value_in_arguments_is_denied(conn, monkeypatch):                   # Ruling 23
    monkeypatch.setenv("ABLIT_API_KEY", KEY)
    tool, ran = fake_tool("t_echo", schema=ECHO); registry.register(tool)
    chat, run_id = await seeded_run(conn)
    out, events, steps = await go(conn, chat, run_id, ToolCall("c1", "t_echo", {"text": f"send {KEY}"}))
    assert ran == [] and out.block.is_error and not out.invalid and "permission policy (secret)" in out.block.content
    assert [(s.kind, s.status, s.detail["rule"]) for s in steps] == [("permission", "denied", "secret")]
    assert all(e["type"] != "permission_request" for e in events)
    bad = await go(conn, chat, run_id, ToolCall("c2", "t_echo", {}, error=f"arguments are not valid JSON (received: {KEY})"))
    assert "[redacted]" in bad[0].block.content
    seen = json.dumps(events + bad[1]) + json.dumps([s.model_dump() for s in bad[2]]) + out.block.content + bad[0].block.content
    assert KEY not in seen

async def test_hidden_characters_force_an_ask_without_a_chat_grant(conn):             # Ruling 23
    tool, ran = fake_tool("t_echo", schema=ECHO, grant_key="text"); registry.register(tool)   # read risk: no prompt otherwise
    chat, run_id = await seeded_run(conn)
    args = {"text": "rm ‮ x"}
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "t_echo", args)))
    req = await answer_next(run_id, "allow_chat"); await task
    assert runner.HIDDEN_WARNING in req.warnings and req.can_allow_chat is False and req.arguments == args
    assert ran == [args] and await grants.list(conn, chat.id) == []
    assert runner.has_hidden({"a": ["x​y"]}) and runner.has_hidden("a\x1b[0m") and not runner.has_hidden({"a": "line\n\ttab"})

async def test_policy_change_mid_run_applies_to_the_next_call(conn):                  # Ruling 22
    tool, ran = fake_tool("t_echo", schema=ECHO); registry.register(tool)
    ex, ex_ran = fake_tool("t_exec", risk="execute", grant_key="command"); registry.register(ex)
    chat, run_id = await seeded_run(conn)
    offered = {"t_echo", "t_exec"}                                                     # what the model was given at run start
    out, _, _ = await go(conn, chat, run_id, ToolCall("c1", "t_echo", {"text": "hi"}), offered=offered)
    assert out.block.content == "ok"
    await chats.update(conn, chat.id, permissions={"tools": {"t_echo": "deny"}})
    out, _, _ = await go(conn, chat, run_id, ToolCall("c2", "t_echo", {"text": "hi"}), offered=offered)
    assert "permission policy (chat-tool)" in out.block.content and not out.invalid and ran == [{"text": "hi"}]
    await chats.update(conn, chat.id, mode="plan", permissions={})
    out, _, steps = await go(conn, chat, run_id, ToolCall("c3", "t_exec", {"command": "id"}), offered=offered)
    assert "permission policy (mode)" in out.block.content and not out.invalid and ex_ran == []
    assert [s.status for s in steps if s.kind == "permission"] == ["done", "denied", "denied"]
    out, _, _ = await go(conn, chat, run_id, ToolCall("c4", "t_exec", {"command": "id"}))    # a run started in plan mode is not offered it
    assert out.invalid and 'Unknown tool "t_exec"' in out.block.content

async def test_write_after_an_execute_grant_makes_the_command_ask_again(conn, tmp_path):     # Ruling 20
    ex, ran = fake_tool("t_exec", risk="execute", grant_key="command"); registry.register(ex)
    wr, _ = fake_tool("t_write", risk="write", prepared=Prepared(paths=(PathUse(str(tmp_path / "run.sh"), "write"),), grant_target="w"))
    registry.register(wr)
    chat, run_id = await seeded_run(conn)
    first = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "t_exec", {"command": "./run.sh"})))
    await answer_next(run_id, "allow_chat"); await first
    await go(conn, chat, run_id, ToolCall("c2", "t_exec", {"command": "./run.sh"}))            # granted: no ask
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c3", "t_write", {})))
    await answer_next(run_id, "allow_once"); _, _, steps = await task
    assert steps[-1].detail["grants_cleared"] == 1 and await grants.list(conn, chat.id) == []
    again = asyncio.create_task(go(conn, chat, run_id, ToolCall("c4", "t_exec", {"command": "./run.sh"})))
    req = await answer_next(run_id, "deny"); await again                                       # the same command asks again
    assert req.arguments == {"command": "./run.sh"} and len(ran) == 2

async def test_names_are_resolved_only_after_the_decision(conn, dns):                         # Review Focus 4, Ruling 19
    table, lookups = dns; table["evil.test"] = ["203.0.113.9"]; table["rebind.test"] = ["127.0.0.1"]
    def net(host):
        t, ran = fake_tool("t_" + host.split(".")[0], risk="network",
                           prepared=Prepared(hosts=(HostUse(host, 80),), grant_target=f"GET http://{host}:80", resolve_hosts=True))
        registry.register(t); return t.name, ran
    (evil, evil_ran), (rebind, rebind_ran) = net("evil.test"), net("rebind.test")
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", evil, {})))
    req = await answer_next(run_id, "deny"); await task
    assert req.hosts == [{"host": "evil.test", "port": 80, "ips": []}] and runner.NOT_RESOLVED in req.reasons
    assert lookups == [] and evil_ran == []                                                    # a denied call sends no DNS query
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c2", evil, {})))
    await answer_next(run_id, "allow_once"); _, _, steps = await task
    assert lookups == ["evil.test"] and evil_ran == [{}]
    assert steps[-1].detail["hosts"] == [{"host": "evil.test", "port": 80, "ips": ["203.0.113.9"]}]
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c3", rebind, {})))
    first = await answer_next(run_id, "allow_chat")                                            # the name only
    second = await answer_next(run_id, "deny")                                                 # the address brought a new warning
    await task
    assert first.hosts[0]["ips"] == [] and second.hosts[0]["ips"] == ["127.0.0.1"]
    assert any("resolves to 127.0.0.1" in w for w in second.warnings) and rebind_ran == []
    assert await grants.list(conn, chat.id) == []                                              # the grant waits for the last answer
    await chats.update(conn, chat.id, permissions={"risk": {"network": "allow"}}); lookups.clear()
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c4", rebind, {})))             # allowed by policy: resolved first, one prompt
    only = await answer_next(run_id, "deny"); await task
    assert only.hosts[0]["ips"] == ["127.0.0.1"] and lookups == ["rebind.test"] and rebind_ran == []

async def test_tool_denied_by_policy_is_not_offered(conn):
    tool, ran = fake_tool("t_echo", schema=ECHO)
    registry.register(tool)
    chat, run_id = await seeded_run(conn, permissions={"tools": {"t_echo": "deny"}})
    out, _, _ = await go(conn, chat, run_id, ToolCall("c1", "t_echo", {"text": "hi"}))
    assert out.invalid and 'Unknown tool "t_echo"' in out.block.content and ran == []


async def test_deny_decided_from_the_prepared_call_never_runs_the_handler(conn, tmp_path, monkeypatch):
    env = tmp_path / "secret.env"
    env.write_text("K=v")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    tool, ran = fake_tool("t_read", prepared=Prepared(paths=(PathUse(str(env), "read"),)))
    registry.register(tool)
    chat, run_id = await seeded_run(conn)
    out, events, steps = await go(conn, chat, run_id, ToolCall("c1", "t_read", {}))
    assert "Denied by HunterAI's permission policy (protected)" in out.block.content
    assert out.block.is_error and not out.invalid and ran == []
    assert [(s.kind, s.status) for s in steps] == [("permission", "denied")]
    assert events[-1]["type"] == "tool_finished" and events[-1]["is_error"] is True


async def test_ask_publishes_the_exact_call_and_allow_once_runs(conn):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)
    chat, run_id = await seeded_run(conn)
    args = {"command": "nmap -sV 10.0.0.5"}
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "t_exec", args)))
    await answer_next(run_id, "allow_once")
    out, events, steps = await task
    asked = next(e for e in events if e["type"] == "permission_request")["request"]
    assert asked["arguments"] == args and asked["tool"] == "t_exec" and asked["risk"] == "execute"
    assert asked["reasons"] and asked["run_id"] == run_id and asked["tool_call_id"] == "c1"
    resolved = next(e for e in events if e["type"] == "permission_resolved")
    assert resolved["answer"] == "allow_once" and resolved["request_id"] == asked["id"]
    assert ran == [args] and await grants.list(conn, chat.id) == []
    assert steps[0].detail["answer"] == "allow_once" and out.block.content == "ok"
    types = [e["type"] for e in events]
    assert types.index("permission_request") < types.index("permission_resolved") < types.index("tool_started")


async def test_allow_chat_is_downgraded_when_not_offered(conn):
    tool, ran = fake_tool("t_exec", risk="execute")         # no grant target
    registry.register(tool)
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "t_exec", {})))
    req = await answer_next(run_id, "allow_chat")
    await task
    assert req.can_allow_chat is False and ran == [{}] and await grants.list(conn, chat.id) == []


async def test_handler_errors_become_results_and_are_scrubbed(conn, monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", KEY)
    a, _ = fake_tool("t_a", raises=ToolError("bad path"))
    b, _ = fake_tool("t_x", raises=RuntimeError(f"key {KEY}"))
    registry.register(a)
    registry.register(b)
    chat, run_id = await seeded_run(conn)
    out, _, steps = await go(conn, chat, run_id, ToolCall("c1", "t_a", {}))
    assert out.block.content == "bad path" and out.block.is_error and not out.invalid
    assert steps[-1].status == "error" and steps[-1].detail["is_error"] is True
    out, _, _ = await go(conn, chat, run_id, ToolCall("c2", "t_x", {}))
    assert out.block.content == "Tool t_x failed: RuntimeError: key [redacted]"


async def test_unresolvable_name_is_an_error_result(conn, dns):
    tool, ran = fake_tool("t_net", risk="network", prepared=Prepared(
        hosts=(HostUse("nope.test", 80),), grant_target="GET http://nope.test:80", resolve_hosts=True))
    registry.register(tool)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"network": "allow"}})
    out, _, _ = await go(conn, chat, run_id, ToolCall("c1", "t_net", {}))
    assert "cannot resolve nope.test" in out.block.content and out.block.is_error and ran == []


async def test_cancel_while_waiting_closes_the_permission_step(conn):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "t_exec", {"command": "id"})))
    while not permissions.pending(run_id):
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    steps = (await runs.get(conn, run_id)).steps
    assert [(s.kind, s.status) for s in steps] == [("permission", "cancelled")]
    assert steps[0].ended_at and permissions.pending() == [] and ran == []


async def test_interrupt_all_closes_the_step_interrupted(conn):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)
    chat, run_id = await seeded_run(conn)
    publish, events = toolkit.collect()
    task = asyncio.create_task(runner.call(
        conn, chat_id=chat.id, run_id=run_id, tc=ToolCall("c1", "t_exec", {"command": "id"}),
        offered={"t_exec"}, publish=publish))
    while not permissions.pending(run_id):
        await asyncio.sleep(0.01)
    assert permissions.interrupt_all() == {run_id}
    with pytest.raises(permissions.Interrupted):
        await task
    steps = (await runs.get(conn, run_id)).steps
    assert [(s.kind, s.status) for s in steps] == [("permission", "interrupted")] and ran == []
    assert [e["answer"] for e in events if e["type"] == "permission_resolved"] == ["interrupted"]
    assert permissions.pending() == []


async def test_answer_rules():
    def req(i, run):
        return permissions.PermissionRequest(
            id=i, run_id=run, chat_id="c", tool_call_id="t", tool="x", risk="execute",
            source="native", arguments={}, created_at="x")
    a = asyncio.create_task(permissions.wait(req("r1", "runA")))
    await asyncio.sleep(0)
    b = asyncio.create_task(permissions.wait(req("r2", "runB")))
    await asyncio.sleep(0)
    assert [p.id for p in permissions.pending()] == ["r1", "r2"]
    assert [p.id for p in permissions.pending("runB")] == ["r2"]
    assert permissions.get("runA", "r1").id == "r1" and permissions.get("runB", "r1") is None
    allow = permissions.Answer("allow_once")
    assert permissions.answer("runA", "nope", allow) is False
    assert permissions.answer("runB", "r1", allow) is False            # wrong run
    assert permissions.answer("runA", "r1", allow) is True
    assert permissions.answer("runA", "r1", permissions.Answer("deny")) is False   # already answered
    assert await a == allow
    assert permissions.answer("runB", "r2", permissions.Answer("deny", "no")) is True
    assert (await b).note == "no" and permissions.pending() == []


async def test_patch_step_merges_detail(conn):
    chat, run_id = await seeded_run(conn)
    step = await runs.add_step(conn, run_id, "tool_call", "t", {"a": 1})
    step = await runs.patch_step(conn, step.id, detail={"b": 2})
    assert step.detail == {"a": 1, "b": 2} and step.status == "running" and step.ended_at is None
    step = await runs.patch_step(conn, step.id, status="done", detail={"a": 3}, close=True)
    assert step.detail == {"a": 3, "b": 2} and step.status == "done" and step.ended_at
