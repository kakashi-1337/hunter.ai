import os
import re
from pathlib import Path

import pytest

from hunterai import config, procs
from hunterai.providers.base import ToolCall
from hunterai.repo import chats, grants
from hunterai.tools import registry, shell
from tests.toolkit import answer_next, eventually, run_call, seeded_run

import asyncio

ALLOW = {"risk": {"execute": "allow"}}


@pytest.fixture(autouse=True)
def shell_registered():
    for t in shell.tools():
        registry.register(t)


def call(conn, chat, run_id, tool, cid="c", **args):
    return run_call(conn, chat, run_id, ToolCall(cid, tool, args))


async def ask(conn, chat, run_id, tool, decision, **args):
    task = asyncio.create_task(call(conn, chat, run_id, tool, **args))
    req = await answer_next(run_id, decision)
    out, _, steps = await task
    return req, out.block, steps


async def test_execute_result_and_exit_codes(conn):
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    out, _, steps = await call(conn, chat, run_id, "execute", command="echo hi; exit 3")
    lines = out.block.content.splitlines()
    assert lines[0] == "exit code: 3" and lines[1].startswith("duration: ") and "hi" in lines
    assert lines[2] == f"cwd: {chats.resolve_workdir(chat)}" and not out.block.is_error
    assert steps[-1].detail["exit_code"] == 3 and steps[-1].detail["process_id"]
    assert steps[-1].detail["full_output_path"].endswith(".log")


async def test_execute_timeout_result(conn, monkeypatch):
    monkeypatch.setattr(procs, "KILL_GRACE", 0.3)
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    out, _, _ = await call(conn, chat, run_id, "execute", command="sleep 30", timeout=1)
    assert out.block.is_error and out.block.content.startswith("timed out after 1s")
    assert procs.running(chat.id) == []


async def test_execute_asks_with_exact_command_cwd_and_hosts(conn):
    chat, run_id = await seeded_run(conn)                               # pentesting: execute asks
    cwd = str(chats.resolve_workdir(chat))
    req, block, _ = await ask(conn, chat, run_id, "execute", "deny", command="nmap 10.0.0.5")
    assert req.arguments == {"command": "nmap 10.0.0.5"} and req.cwd == cwd
    assert req.hosts == [{"host": "10.0.0.5", "port": None, "ips": ["10.0.0.5"]}]
    assert any("10.0.0.5 is an address on this machine or local network" in w for w in req.warnings)
    assert req.grant_target == f"{cwd}\nnmap 10.0.0.5" and procs.running(chat.id) == []
    assert block.is_error and "denied" in block.content


async def test_allow_for_chat_does_not_cover_another_command_or_cwd(conn):
    chat, run_id = await seeded_run(conn)
    _, block, _ = await ask(conn, chat, run_id, "execute", "allow_chat", command="id")
    assert block.content.startswith("exit code: 0")
    out, events, _ = await call(conn, chat, run_id, "execute", command="id")       # same: no ask
    assert all(e["type"] != "permission_request" for e in events) and not out.block.is_error
    req, _, _ = await ask(conn, chat, run_id, "execute", "deny", command="id; whoami")
    assert req.arguments["command"] == "id; whoami"
    req, _, _ = await ask(conn, chat, run_id, "execute", "deny", command="id", cwd="/tmp")
    assert req.cwd == "/tmp" and len(await grants.list(conn, chat.id)) == 1


async def test_out_of_scope_host_in_a_command_warns(conn):
    chat, run_id = await seeded_run(conn, scope={"in": ["*.acme.test"], "out": []}, permissions=ALLOW)
    req, _, _ = await ask(conn, chat, run_id, "execute", "deny", command="curl https://other.test")
    assert any("other.test is not in this target's in-scope list" in w for w in req.warnings)
    assert procs.running(chat.id) == []


async def test_command_naming_the_env_file_is_denied(conn, tmp_path, monkeypatch):
    env = tmp_path / "keys.env"
    env.write_text("ABLIT_API_KEY=ak_secret_value_123\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    for args in ({"command": f"cat {env}"}, {"command": "cat ./keys.env", "cwd": str(tmp_path)},
                 {"command": f"grep KEY<'{env}'"}):
        out, _, steps = await call(conn, chat, run_id, "execute", **args)
        assert "permission policy (protected)" in out.block.content and "ak_secret" not in out.block.content
        assert all(s.kind != "tool_call" for s in steps)
    assert procs.running(chat.id) == []


async def test_command_addressing_the_own_api_always_asks(conn):
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    req, _, _ = await ask(conn, chat, run_id, "execute", "deny",
                          command="curl localhost:8790/api/settings -X PUT")
    assert req.can_allow_chat is False and any("HunterAI's own API" in w for w in req.warnings)


async def test_plan_mode_has_no_execute(conn):
    chat, run_id = await seeded_run(conn, mode="plan")
    out, _, _ = await call(conn, chat, run_id, "execute", command="id")
    assert out.invalid and 'Unknown tool "execute"' in out.block.content


async def test_process_input_always_asks_and_offers_no_chat_grant(conn, monkeypatch):
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    await call(conn, chat, run_id, "execute", command="cat", background=True)
    proc = procs.running(chat.id)[0]
    await chats.update(conn, chat.id, permissions={})
    for text in ("one\n", "two\n"):                                   # every input asks again
        req, block, _ = await ask(conn, chat, run_id, "process_input", "allow_chat", id=proc.id, text=text)
        assert req.can_allow_chat is False and req.cwd == proc.cwd and not block.is_error
    await eventually(lambda: procs.read(proc.log_path)[0] == "one\ntwo\n")
    assert await grants.list(conn, chat.id) == []


async def test_process_tools_are_chat_scoped(conn, monkeypatch):
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    a, run_a = await seeded_run(conn, permissions=ALLOW)
    b, run_b = await seeded_run(conn, permissions=ALLOW)
    await call(conn, a, run_a, "execute", command="sleep 300", background=True)
    proc = procs.running(a.id)[0]
    for tool, args in (("process_output", {}), ("process_input", {"text": "x"}), ("process_kill", {})):
        out, _, _ = await call(conn, b, run_b, tool, id=proc.id, **args)
        assert out.block.is_error and "no such process" in out.block.content
    assert [p.id for p in procs.running(a.id)] == [proc.id]


async def test_foreground_output_is_bounded(conn, monkeypatch):
    monkeypatch.setattr(procs, "FOREGROUND_BYTES", 1000)
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    out, _, steps = await call(conn, chat, run_id, "execute",
                               command="head -c 5000 /dev/zero | tr '\\0' x")
    log = steps[-1].detail["full_output_path"]
    assert f"[output continues: {log}]" in out.block.content and os.path.getsize(log) == 5000
    assert out.block.content.count("x") < 1100


async def test_background_limit_per_chat(conn, monkeypatch):
    monkeypatch.setattr(procs, "MAX_PROCS_PER_CHAT", 1)
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    first, _, _ = await call(conn, chat, run_id, "execute", command="sleep 300", background=True)
    second, _, _ = await call(conn, chat, run_id, "execute", command="sleep 300", background=True)
    assert "started background process" in first.block.content
    assert second.block.is_error and "background processes" in second.block.content
    assert len(procs.running(chat.id)) == 1
