import asyncio
import os
from pathlib import Path

import httpx
import pytest

from hunterai import config, db, engine, mcp_manager, procs
from hunterai.api.app import create_app
from hunterai.repo import runs as runs_repo
from hunterai.tools import native, registry
from tests.test_api import BASE, client, new_chat, send  # noqa: F401 (client is a fixture)
from tests.toolkit import alive, eventually, fake_tool


async def wait_for_ask(client, run_id, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        waiting = (await client.get(f"/api/runs/{run_id}/permissions")).json()
        if waiting:
            return waiting[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no permission request was asked")


NATIVE = sorted(["kb_search", "kb_files", "kb_read", "cve", "mcp_find", "mcp_call", "profundis", "look_at_image", "caido", "caido_send", "browser_navigate", "browser_click", "browser_type", "browser_read", "browser_screenshot", "browser_network", "browser_console", "browser_close", "edit", "execute", "process_input", "process_kill", "process_output", "read", "repo_search",
                 "web_download", "web_fetch", "web_search", "write",
                 "decide_check", "decide_classify", "decide_decide", "decide_gate", "decide_rank", "decide_score",
                 "focus", "critique", "finding_add", "finding_update", "finding_list", "delegate", "swarm", "skill",
                 "encode_decode"])

async def test_native_tools_are_registered_and_filtered_by_mode(client):
    assert [t["name"] for t in (await client.get("/api/tools")).json()] == NATIVE
    plan = await new_chat(client, mode="plan")
    tools = {t["name"]: t for t in (await client.get("/api/tools", params={"chat_id": plan["id"]})).json()}
    assert not tools["execute"]["offered"] and tools["execute"]["rule"] == "mode" and not tools["write"]["offered"]
    assert not tools["web_download"]["offered"] and tools["web_download"]["rule"] == "mode"      # it writes a file
    assert tools["read"]["decision"] == "allow" and tools["web_fetch"]["decision"] == "ask"
    assert tools["encode_decode"]["decision"] == "allow"      # it converts a text and touches nothing

async def test_mock_chat_runs_a_real_native_tool_end_to_end(client):
    chat = await new_chat(client)
    sent = await send(client, chat["id"], 'call repo_search {"pattern": "zzz"}')
    run = (await client.get(f"/api/runs/{sent['run_id']}")).json()
    assert run["status"] == "done"
    assert [s["kind"] for s in run["steps"]] == ["model_call", "permission", "tool_call", "model_call"]
    assert sent["events"][-1]["message"]["blocks"][-1]["text"].startswith("tool said: root: ")
    assert run["totals"]["turns"] == 2 and run["totals"]["calls"] == 1


def test_register_all_is_idempotent_and_keeps_other_sources():
    registry.register(fake_tool("t_other")[0])
    native.register_all()
    native.register_all()
    names = [t.name for t in registry.all()]
    assert names == sorted(NATIVE + ["t_other"]) and len([t for t in registry.all() if t.source == "native"]) == len(NATIVE)


async def test_settings_permissions_and_max_turns(client):
    s = (await client.get("/api/settings")).json()
    assert (s["permissions"], s["max_turns"]) == ({}, 25) and s["presets"]["plan"]["execute"] == "deny"
    r = await client.put("/api/settings", json={"permissions": {"risk": {"network": "allow"}}})
    assert r.json()["permissions"] == {"risk": {"network": "allow"}}
    assert (await client.put("/api/settings", json={"permissions": {}})).json()["permissions"] == {}
    assert (await client.put("/api/settings", json={"permissions": {"risk": {"network": "maybe"}}})).status_code == 400
    for bad in (0, 201):
        assert (await client.put("/api/settings", json={"max_turns": bad})).status_code == 400
    assert (await client.put("/api/settings", json={"max_turns": 40})).json()["max_turns"] == 40
    await client.put("/api/settings", json={"max_turns": 1})
    chat = await new_chat(client)
    sent = await send(client, chat["id"], 'call repo_search {"pattern": "zzz"}')
    assert sent["events"][-1]["error"].startswith("Stopped after 1 model turns")


async def test_chat_workdir_rules(client, tmp_path):
    chat = await new_chat(client)
    url = f"/api/chats/{chat['id']}"
    assert (await client.get(url)).json()["workdir"] == str(config.home() / "work" / chat["id"])
    for bad in ("relative/x", "/", str(Path.home()), str(config.home()), str(tmp_path),
                str(config.PROJECT_ROOT / "anbu-system-one" / "w")):
        assert (await client.patch(url, json={"workdir": bad})).status_code == 400, bad
    assert not (config.PROJECT_ROOT / "anbu-system-one" / "w").exists()
    (tmp_path / "proj").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "proj")
    assert (await client.patch(url, json={"workdir": str(tmp_path / "link")})).json()["workdir"] == str(tmp_path / "proj")
    assert (await client.patch(url, json={"workdir": str(tmp_path / "new")})).status_code == 200
    assert (tmp_path / "new").is_dir() and (await client.get(url)).json()["workdir"] == str(tmp_path / "new")
    assert (await client.patch(url, json={"workdir": ""})).json()["workdir"] is None
    assert (await client.get(url)).json()["workdir"] == str(config.home() / "work" / chat["id"])


async def test_chat_permissions_patch_and_grants_routes(client):
    target = (await client.post("/api/targets", json={"name": "t2"})).json()
    chat = await new_chat(client)
    url = f"/api/chats/{chat['id']}"
    assert (await client.patch(url, json={"permissions": {"bypass": True}})).status_code == 400
    assert (await client.patch(url, json={"permissions": {"domains": ["x.test"]}})).json()["permissions"] == {"domains": ["x.test"]}

    async def grant():
        sent = await send(client, chat["id"], 'call execute {"command": "true"}', wait=False)
        req = await wait_for_ask(client, sent["run_id"])
        await client.post(f"/api/runs/{sent['run_id']}/permissions/{req['id']}", json={"decision": "allow_chat"})
        await engine.wait(sent["run_id"])
    await grant()
    listed = (await client.get(f"{url}/grants")).json()
    assert [(g["tool"], g["warned"]) for g in listed] == [("execute", False)] and listed[0]["target"].endswith("\ntrue")
    assert (await client.delete(f"{url}/grants")).status_code == 204
    assert (await client.get(f"{url}/grants")).json() == []
    for change in ({"target_id": target["id"]}, {"mode": "coding"}):
        await grant()
        assert len((await client.get(f"{url}/grants")).json()) == 1
        assert (await client.patch(url, json=change)).status_code == 200
        assert (await client.get(f"{url}/grants")).json() == []      # the same call asks again
    await client.patch(url, json={"title": "renamed"})                 # an unrelated change keeps grants
    await grant()
    await client.patch(url, json={"title": "again", "mode": "coding"})
    assert len((await client.get(f"{url}/grants")).json()) == 1


async def test_process_routes(client, monkeypatch):
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    chat = await new_chat(client)
    await client.patch(f"/api/chats/{chat['id']}", json=ALLOW)
    await send(client, chat["id"], 'call execute {"command": "echo ready; read x; echo got:$x", "background": true}')
    listed = (await client.get(f"/api/chats/{chat['id']}/processes")).json()
    pid = listed[0]["id"]
    assert listed[0]["status"] == "running" and listed[0]["background"] is True
    out = (await client.get(f"/api/processes/{pid}/output")).json()
    assert out["text"] == "ready\n" and out["next_offset"] == out["total"] == 6 and out["status"] == "running"
    assert (await client.post(f"/api/processes/{pid}/input", json={"text": "hi\n"})).json() == {"ok": True}
    await eventually(lambda: not procs.running(chat["id"]))
    out = (await client.get(f"/api/processes/{pid}/output", params={"offset": 6})).json()
    assert (out["text"], out["status"], out["exit_code"]) == ("got:hi\n", "exited", 0)
    assert (await client.post(f"/api/processes/{pid}/input", json={"text": "x"})).status_code == 409
    for path in ("/api/processes/nope/output", "/api/processes/nope/input", "/api/processes/nope/kill"):
        r = await (client.get(path) if path.endswith("output") else client.post(path, json={"text": "x"}))
        assert r.status_code == 404
    await send(client, chat["id"], 'call execute {"command": "sleep 300", "background": true}')
    live = procs.running(chat["id"])[0]
    assert (await client.post(f"/api/processes/{live.id}/kill")).json()["status"] == "killed"
    procs.reset()                                                      # as after a restart
    out = (await client.get(f"/api/processes/{pid}/output")).json()
    assert out["text"] == "ready\ngot:hi\n" and out["status"] == "exited"


async def test_mcp_status_route(client):
    got = (await client.get("/api/mcp")).json()
    assert got["servers"] == [] and got["error"] is None
    assert [c["name"] for c in got["configured"]] == ["chunker", "rea", "context7"]


async def test_app_starts_when_the_chunker_cannot(monkeypatch, isolated_home, tmp_path):
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    monkeypatch.delenv("HUNTERAI_MCP")
    monkeypatch.setenv("HUNTERAI_CHUNKER", "/nonexistent/x.py")
    monkeypatch.setattr(mcp_manager, "INIT_TIMEOUT", 1)
    (tmp_path / "f.txt").write_text("plain line\n")
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            servers = (await c.get("/api/mcp")).json()["servers"]
            assert servers[0]["name"] == "chunker" and servers[0]["state"] != "ready"
            chat = await new_chat(c)
            sent = await send(c, chat["id"], 'call read {"path": "%s"}' % (tmp_path / "f.txt"))
            result = sent["events"][-1]["message"]["blocks"][-2]["content"]
            assert result.startswith("[chunker unavailable (") and "1\tplain line" in result


BG = 'call execute {"command": "sleep 300", "background": true}'
ALLOW = {"permissions": {"risk": {"execute": "allow"}}}

async def test_delete_chat_kills_processes_and_removes_its_files(client, tmp_path, monkeypatch):      # Ruling 14
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    async def chat_with_sleep(**patch):
        chat = await new_chat(client)
        assert (await client.patch(f"/api/chats/{chat['id']}", json={**ALLOW, **patch})).status_code == 200
        sent = await send(client, chat["id"], BG)
        return chat, sent, procs.running(chat["id"])[0]
    chat, sent, proc = await chat_with_sleep()
    work, log, out = config.home() / "work" / chat["id"], Path(proc.log_path), config.output_dir() / sent["run_id"]
    out.mkdir(parents=True); (out / "s.txt").write_text("x")
    assert work.is_dir() and log.is_file()
    assert (await client.delete(f"/api/chats/{chat['id']}")).status_code == 204
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)
    assert not work.exists() and not log.exists() and not out.exists()
    custom = tmp_path / "proj"; custom.mkdir(); (custom / "keep.txt").write_text("mine")
    chat, _, proc = await chat_with_sleep(workdir=str(custom))
    assert (await client.delete(f"/api/chats/{chat['id']}")).status_code == 204
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)
    assert (custom / "keep.txt").read_text() == "mine"                    # a custom folder is never deleted

async def test_app_stop_kills_processes_and_mcp_children(monkeypatch, isolated_home):                 # Review Focus 2
    monkeypatch.setenv("HUNTERAI_MOCK", "1"); monkeypatch.delenv("HUNTERAI_MCP")
    monkeypatch.setenv("HUNTERAI_CHUNKER", str(Path(__file__).parent / "fake_mcp_server.py"))
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            assert (await c.get("/api/mcp")).json()["servers"][0]["state"] == "ready"
            child = int((await mcp_manager.get().call_tool("chunker", "spawn_child", {})).text)
            chat = await new_chat(c)
            await c.patch(f"/api/chats/{chat['id']}", json=ALLOW)
            sent = await send(c, chat["id"], BG)
            proc = procs.running(chat["id"])[0]
            assert alive(child) and alive(proc.pid)
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)       # leaving the lifespan is the app stopping
    await eventually(lambda: not alive(child))
    conn = await db.connect()
    try:
        step = [s for s in (await runs_repo.get(conn, sent["run_id"])).steps if s.kind == "tool_call"][0]
        assert step.ended_at and step.detail["process_status"] == "killed"
    finally:
        await conn.close()

async def test_processes_die_even_when_engine_shutdown_fails(monkeypatch, isolated_home):
    monkeypatch.setenv("HUNTERAI_MOCK", "1"); monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.1)
    async def boom(): raise RuntimeError("shutdown failed")
    monkeypatch.setattr(engine, "shutdown", boom)
    app = create_app()
    with pytest.raises(RuntimeError, match="shutdown failed"):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
                chat = await new_chat(c)
                await c.patch(f"/api/chats/{chat['id']}", json=ALLOW)
                await send(c, chat["id"], BG)
                proc = procs.running(chat["id"])[0]
    with pytest.raises(ProcessLookupError): os.killpg(proc.pgid, 0)
