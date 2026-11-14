"""Findings of the phase 2a final review, each pinned by the scenario that showed it."""
import asyncio
import os
from pathlib import Path

import pytest

from hunterai import config, permissions, procs
from hunterai.permissions import Policy
from hunterai.providers.base import ToolCall
from hunterai.repo import chats, grants
from hunterai.tools import registry, resolve
from hunterai.tools.base import HostUse, Prepared, Tool, ToolError, ToolResult
from hunterai.types import Target
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)
from tests.toolkit import answer_next, fake_tool, run_call, seeded_run

NET = Policy(risk={"network": "allow"})


def net_tool(host):
    tool, ran = fake_tool("t_net", risk="network", prepared=Prepared(
        hosts=(HostUse(host, 443),), grant_target=f"GET https://{host}:443", resolve_hosts=True))
    registry.register(tool)
    return ran


async def test_allow_once_on_the_warning_prompt_leaves_no_grant(conn, dns):
    dns[0]["rebind.test"] = ["192.168.1.1"]
    ran = net_tool("rebind.test")
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "t_net", {})))
    await answer_next(run_id, "allow_chat")         # the name only, no warning yet
    second = await answer_next(run_id, "allow_once")   # the local-address warning: once
    await task
    assert second.warnings and ran == [{}]
    assert await grants.list(conn, chat.id) == []      # "once" must not become a standing grant


async def test_allow_chat_on_the_warning_prompt_is_a_warned_grant(conn, dns):
    dns[0]["rebind.test"] = ["192.168.1.1"]
    net_tool("rebind.test")
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "t_net", {})))
    await answer_next(run_id, "allow_once")
    await answer_next(run_id, "allow_chat")
    await task
    assert [(g.tool, g.warned) for g in await grants.list(conn, chat.id)] == [("t_net", True)]


async def test_a_failed_start_leaves_no_process_and_shutdown_returns(conn, tmp_path, monkeypatch):
    chat, run_id = await seeded_run(conn)
    real = procs.db.connect

    async def broken():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(procs.db, "connect", broken)
    with pytest.raises(RuntimeError):
        await procs.start(chat_id=chat.id, run_id=run_id, step_id="", cwd=str(tmp_path),
                          command="sleep 300", background=False)
    monkeypatch.setattr(procs.db, "connect", real)
    started = list(procs._live.values())[-1]           # the process that was spawned
    await asyncio.wait_for(procs.shutdown(), 5)        # must not wait for ever
    assert procs.running() == [] and started.reaper is not None and started.proc.status == "killed"
    with pytest.raises(ProcessLookupError):
        os.killpg(started.proc.pgid, 0)


async def test_prepare_that_raises_anything_is_an_error_result(conn):
    async def prepare(ctx, args):
        raise OSError(36, "File name too long")

    async def handler(ctx, args):
        return ToolResult("never")
    registry.register(Tool("t_bad", "d", {"type": "object"}, "read", handler, prepare=prepare))
    chat, run_id = await seeded_run(conn)
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", "t_bad", {}))
    assert out.invalid and out.block.is_error and "Invalid arguments for t_bad" in out.block.content


def test_url_path_refuses_what_httpx_cannot_parse():
    with pytest.raises(ToolError):
        resolve.url_path("http://a.test/\x00x")
    with pytest.raises(ToolError):
        resolve.url_path("http://a.test/" + "a" * 70_000)


@pytest.mark.parametrize("path", ["/Admin/x", "/admin/x", "//admin/x", "/ADMIN", "/admin//x"])
def test_out_of_scope_paths_match_loosely(path):
    t = Target(id="t", name="n", notes="", scope={"in": [], "out": ["example.com/Admin"]}, created_at="x")
    assert resolve.scope_verdict(t, [HostUse("example.com", 443)], path).out == ("example.com",)


def test_in_scope_paths_stay_strict_and_other_paths_are_not_out():
    t = Target(id="t", name="n", notes="", scope={"in": ["example.com/App"], "out": ["example.com/admin"]},
               created_at="x")
    h = [HostUse("example.com", 443)]
    assert resolve.scope_verdict(t, h, "/App/x") == resolve.ScopeVerdict((), ())
    assert resolve.scope_verdict(t, h, "/app/x").not_in == ("example.com",)
    assert resolve.scope_verdict(t, h, "/administrator").out == ()


def test_browser_profiles_and_other_credential_stores_ask():
    home = Path.home()
    for p in (home / "snap/firefox/common/.mozilla/firefox/x/logins.json", home / ".pgpass", home / ".my.cnf",
              home / ".config/rclone/rclone.conf", home / ".azure/token", home / ".vault-token",
              home / "snap/chromium/common/chromium/Default/Cookies", home / ".thunderbird/p/key4.db",
              home / ".config/BraveSoftware/x", home / ".terraform.d/credentials.tfrc.json"):
        assert "usually holds credentials" in (resolve.sensitive(p) or ""), p
    for p in (home / ".zshenv", home / ".zprofile", home / ".bash_logout", home / ".gitconfig",
              home / ".config/fish/config.fish"):
        assert "can run code later" in (resolve.sensitive(p, "write") or ""), p
        assert resolve.sensitive(p, "read") is None


async def test_deleting_an_unknown_chat_removes_nothing(client):
    chat = await new_chat(client)
    work = config.home() / "work" / chat["id"]
    work.mkdir(parents=True)
    (work / "keep.txt").write_text("mine")
    assert (await client.delete("/api/chats/nope")).status_code == 404
    from hunterai.api import chats as chats_api
    from fastapi import HTTPException
    conn = await __import__("hunterai").db.connect()
    try:
        for cid in (".", "..", ""):
            with pytest.raises(HTTPException):
                await chats_api.delete_chat(cid, conn)
    finally:
        await conn.close()
    assert (work / "keep.txt").read_text() == "mine" and config.db_path().exists()


def test_a_domain_name_entry_does_not_silence_the_local_address_warning(tmp_path):
    tool, _ = fake_tool("t", risk="network")
    kw = dict(mode="pentesting", tool=tool, target=None, workdir=tmp_path, global_policy=Policy(), grants=[])
    rebound = Prepared(hosts=(HostUse("x.target.test", 11434, ("127.0.0.1",)),), grant_target="g")
    named = Policy(risk={"network": "allow"}, domains=["*.target.test"])
    v = permissions.decide(prepared=rebound, chat_policy=named, **kw)
    assert v.decision == "ask" and any("resolves to 127.0.0.1" in w for w in v.warnings)
    by_ip = Policy(risk={"network": "allow"}, domains=["127.0.0.1:11434"])
    assert permissions.decide(prepared=rebound, chat_policy=by_ip, **kw).decision == "allow"
    local = Prepared(hosts=(HostUse("localhost", 3000),), grant_target="l")
    assert permissions.decide(prepared=local, chat_policy=Policy(risk={"network": "allow"}, domains=["localhost"]), **kw).decision == "allow"
