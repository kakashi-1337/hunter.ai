import asyncio
import os
import re
import time
from types import SimpleNamespace

import pytest

from hunterai import db, engine, procs
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import chats, runs
from hunterai.tools import registry, shell
from hunterai.tools.base import ToolError
from tests.test_engine import no_bytes, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, alive, eventually, run_call, seeded_run, slow_connections, still_open

ALLOW = {"risk": {"execute": "allow"}}


@pytest.fixture
async def start(conn, tmp_path):
    """start(command, background=False): a process of a seeded chat, in tmp_path."""
    chat, run_id = await seeded_run(conn)

    async def go(command, background=False):
        return await procs.start(chat_id=chat.id, run_id=run_id, step_id="", command=command,
                                 cwd=str(tmp_path), background=background)
    go.chat = chat
    return go


@pytest.fixture
def shell_registered():
    for t in shell.tools():
        registry.register(t)


async def test_timeout_kills_a_child_that_ignores_sigterm(conn, start, monkeypatch):            # Review Focus 2
    monkeypatch.setattr(procs, "KILL_GRACE", 0.3)
    p = await start("trap '' TERM; while :; do sleep 0.1; done")
    t0 = time.monotonic(); done = await procs.wait(p.id, timeout=0.3)
    assert done.status == "killed" and time.monotonic() - t0 < 5
    with pytest.raises(ProcessLookupError): os.killpg(p.pgid, 0)

async def test_leftover_grandchild_cannot_hang_the_call_and_is_killed(conn, start, tmp_path):   # Review Focus 2
    p = await start("(sleep 300 & echo $! > gc.pid); echo started")
    done = await asyncio.wait_for(procs.wait(p.id, timeout=10), 5)      # returns although the grandchild holds the output file open
    assert (done.status, done.exit_code) == ("exited", 0) and "started" in procs.read(p.log_path)[0]
    await eventually(lambda: not alive(int((tmp_path / "gc.pid").read_text())))

async def test_cancel_mid_command_kills_the_group_and_closes_the_step(conn, setup):             # Review Focus 2, through the engine
    for t in shell.tools(): registry.register(t)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "execute", {"command": "sleep 300"})], [TextDelta("unreached")]))
    chat, user = await setup()
    await chats.update(conn, chat.id, permissions={"risk": {"execute": "allow"}})
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await eventually(lambda: procs.running(chat.id))
    proc = procs.running(chat.id)[0]
    assert await engine.cancel(run_id); await engine.wait(run_id)
    run = await runs.get(conn, run_id)
    assert run.status == "cancelled" and [(s.kind, s.status) for s in run.steps][-1] == ("tool_call", "cancelled")
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)

async def test_cancel_as_a_command_starts_leaves_no_connection_open(conn, setup, monkeypatch):
    """Stop pressed while the process is being recorded: the connection for that record was left open on a slow disk,
    and its thread kept the whole app from exiting."""
    for t in shell.tools(): registry.register(t)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "execute", {"command": "sleep 300"})], [TextDelta("unreached")]))
    chat, user = await setup()
    await chats.update(conn, chat.id, permissions={"risk": {"execute": "allow"}})
    opened = slow_connections(monkeypatch)
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await eventually(lambda: procs.running(chat.id))             # started, and its record is still being written
    proc = procs.running(chat.id)[0]
    assert await engine.cancel(run_id); await engine.wait(run_id)
    assert (await runs.get(conn, run_id)).status == "cancelled"
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)
    await eventually(lambda: not still_open(opened))

async def test_shutdown_kills_background_processes_and_closes_their_steps(conn, shell_registered):   # Review Focus 2
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    out, _, steps = await run_call(conn, chat, run_id, ToolCall("c1", "execute", {"command": "sleep 300", "background": True}))
    proc = procs.running(chat.id)[0]
    assert "started background process" in out.block.content and steps[-1].status == "running" and steps[-1].ended_at is None
    await procs.shutdown()
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)
    step = (await runs.get(conn, run_id)).steps[-1]
    assert (await procs.get(proc.id)).status == "killed" and procs.running(chat.id) == []
    assert (step.status, step.detail["process_status"]) == ("error", "killed") and step.ended_at

async def test_background_step_closes_when_the_process_exits(conn, shell_registered, monkeypatch):
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    _, _, steps = await run_call(conn, chat, run_id, ToolCall("c1", "execute", {"command": "sleep 0.6; echo done", "background": True}))
    assert steps[-1].status == "running" and steps[-1].detail["process_id"]            # one step, still open
    await eventually(lambda: not procs.running(chat.id))
    for _ in range(100):                                                               # the reaper closes the step on its own connection
        run = await runs.get(conn, run_id)
        if run.steps[-1].ended_at: break
        await asyncio.sleep(0.05)
    step = run.steps[-1]
    assert (step.status, step.detail["exit_code"], step.detail["process_status"]) == ("done", 0, "exited")
    assert run.totals["calls"] == 1 and len([s for s in run.steps if s.kind == "tool_call"]) == 1
    proc = await procs.get(step.detail["process_id"])
    assert "done" in procs.read(proc.log_path)[0]

async def test_child_env_drops_keys(conn, start, monkeypatch):
    for name in ("ABLIT_API_KEY", "OPENAI_API_KEY", "MY_SERVICE_API_KEY", "OTHERTOOL_LLM", "OTHERSVC_API_KEY", "ABLIT_MODEL"):
        monkeypatch.setenv(name, "ak_secret_value_123")
    monkeypatch.setenv("HUNTERAI_PRIVATE_ENV", "OTHERTOOL_, OTHERSVC_")      # the owner's own list of private prefixes
    env = procs.child_env()
    assert not [n for n in env if n.endswith("_API_KEY") or n.startswith(("OTHERTOOL_", "OTHERSVC_", "HUNTERAI_", "ABLIT_"))]
    assert env["PATH"].split(":", 1)[1] == os.environ["PATH"] and "HOME" in env     # the owner's tool folder comes first
    p = await start("env"); await procs.wait(p.id, timeout=10)                         # what a real child sees
    seen = procs.read(p.log_path)[0]
    assert "PATH=" in seen and "ak_secret_value_123" not in seen and "HUNTERAI_HOME" not in seen and "OTHERTOOL" not in seen

async def test_process_output_tool_pages_and_reports_status(conn, shell_registered, monkeypatch):
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", "execute", {"command": "echo a; sleep 1.5; echo b", "background": True}))
    pid = re.search(r'process_output\(id="([^"]+)", offset=\d+\)', out.block.content).group(1)
    await chats.update(conn, chat.id, permissions={})                                  # pentesting defaults: reading output is risk "read"
    first, events, _ = await run_call(conn, chat, run_id, ToolCall("c2", "process_output", {"id": pid}))
    assert "status: running" in first.block.content and "a" in first.block.content.splitlines()
    assert all(e["type"] != "permission_request" for e in events)
    nxt = int(re.search(r'more: process_output\(id="[^"]+", offset=(\d+)\)', first.block.content).group(1))
    second, _, _ = await run_call(conn, chat, run_id, ToolCall("c3", "process_output", {"id": pid, "offset": nxt, "wait_seconds": 5}))
    assert "b" in second.block.content.splitlines() and "a" not in second.block.content.splitlines()
    last, _, _ = await run_call(conn, chat, run_id, ToolCall("c4", "process_output", {"id": pid, "offset": nxt + 2, "wait_seconds": 5}))
    assert "status: exited (code 0)" in last.block.content and "more: process_output" not in last.block.content

async def test_process_kill_tool_ends_the_group(conn, shell_registered, monkeypatch):
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    await run_call(conn, chat, run_id, ToolCall("c1", "execute", {"command": "sleep 300 & sleep 300", "background": True}))
    proc = procs.running(chat.id)[0]
    await chats.update(conn, chat.id, permissions={})                                  # pentesting defaults: kill is risk "read", no ask
    out, events, _ = await run_call(conn, chat, run_id, ToolCall("c2", "process_kill", {"id": proc.id}))
    assert "status: killed" in out.block.content and not out.block.is_error and all(e["type"] != "permission_request" for e in events)
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)

async def test_recover_kills_survivors_and_marks_the_rest_lost(conn, start):
    # A process a previous app instance left behind: alive, with a row, and no handle here.
    import subprocess
    old = subprocess.Popen(["sleep", "300"], start_new_session=True)
    row = ("insert into processes(id,chat_id,pid,pgid,start_ticks,command,cwd,log_path,status,"
           "started_at) values(?,?,?,?,?,'x','/','/nope','running','x')")
    await conn.execute(row, ("alive", start.chat.id, old.pid, old.pid, procs._ticks(old.pid)))
    await conn.execute(row, ("reused", start.chat.id, old.pid, old.pid, 1))      # same pid, another process
    await conn.execute(row, ("dead", start.chat.id, 2 ** 22 - 3, 2 ** 22 - 3, 1))
    await conn.commit()
    try:
        await procs.recover(conn)
        assert old.wait(timeout=5) == -9
    finally:
        old.kill()
    got = {i: (await procs.get(i)).status for i in ("alive", "reused", "dead")}
    assert got == {"alive": "killed", "reused": "lost", "dead": "lost"}


async def test_output_is_read_by_offset(conn, start):
    p = await start("head -c 60000 /dev/zero | tr '\\0' x")
    await procs.wait(p.id, timeout=10)
    text, nxt, total = procs.read(p.log_path)
    assert (len(text), nxt, total) == (24_000, 24_000, 60_000)
    seen = len(text)
    while nxt < total:
        text, nxt, total = procs.read(p.log_path, nxt)
        seen += len(text)
    assert seen == 60_000 and procs.read("/nonexistent/x.log", 5) == ("", 5, 0)


async def test_log_cap_kills_the_process(conn, start, monkeypatch):
    monkeypatch.setattr(procs, "MAX_LOG_BYTES", 10_000)
    p = await start("head -c 300000000 /dev/zero; sleep 300")
    done = await asyncio.wait_for(procs.wait(p.id, timeout=20), 10)
    assert done.status == "killed"
    with open(p.log_path, "rb") as f:
        f.seek(-40, os.SEEK_END)
        assert f.read().decode().endswith("[killed: output passed 0 MiB]\n")
    assert os.path.getsize(p.log_path) < 50 * 1024**2


async def test_low_disk_kills_the_process_and_refuses_new_ones(conn, start, monkeypatch):
    p = await start("sleep 300")
    monkeypatch.setattr(procs.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=1, used=1, free=5 * 1024**3))
    done = await asyncio.wait_for(procs.wait(p.id, timeout=20), 10)
    assert done.status == "killed"
    assert procs.read(p.log_path)[0].endswith("[killed: free disk below 10 GiB]\n")
    with pytest.raises(ToolError, match="not enough free disk"):
        await start("echo no")


async def test_get_and_read_work_after_the_handles_are_gone(conn, start):
    p = await start("echo hi")
    await procs.wait(p.id, timeout=10)
    procs.reset()
    assert (await procs.get(p.id)).status == "exited" and procs.read(p.log_path)[0] == "hi\n"
    assert await procs.get("nope") is None
    for call in (procs.wait(p.id, 1), procs.kill(p.id), procs.send_input(p.id, "x")):
        with pytest.raises(ToolError, match="no such process"):
            await call


async def test_send_input_and_eof(conn, start):
    p = await start("read x; echo got:$x; cat", background=True)
    await procs.send_input(p.id, "hi\n")
    await eventually(lambda: "got:hi" in procs.read(p.log_path)[0])
    await procs.send_input(p.id, "tail\n", eof=True)
    done = await procs.wait(p.id, timeout=10)
    assert done.status == "exited" and procs.read(p.log_path)[0] == "got:hi\ntail\n"
    fg = await start("sleep 5")
    with pytest.raises(ToolError):
        await procs.send_input(fg.id, "x")
    await procs.kill(fg.id)
