"""Orchestrator prompt policy, decision settings, bundled MCP servers and their switch."""
import json
import sys
from pathlib import Path

import pytest

from hunterai import config, mcp_manager
from hunterai.mcp_manager import Manager, ServerConfig
from hunterai.modes import build_system
from hunterai.tools import registry
from tests.test_api import client  # noqa: F401

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def test_orchestrator_rules_follow_the_tools_on_offer():
    full = build_system("pentesting", None, ["read", "focus", "delegate", "finding_add", "decide_check"])
    assert "goal record" in full and "delegate only" in full.lower() and "finding_add" in full
    assert "verified" in full and "decide_" in full
    bare = build_system("pentesting", None, ["read"])
    assert "delegate" not in bare and "finding_add" not in bare and "goal record" not in bare
    assert "subagent" not in build_system("pentesting", None, [])


def test_bundled_servers_are_off_until_turned_on(isolated_home):
    d = mcp_manager.default_servers()
    assert (d["rea"].args, d["rea"].risk, d["rea"].enabled) == (["-y", "rea-agents@6.7.0", "mcp"], "execute", False)
    assert (d["context7"].args, d["context7"].risk, d["context7"].enabled) == (["-y", "@upstash/context7-mcp"], "network", False)
    config.mcp_file().write_text(json.dumps({"servers": {"rea": {"enabled": True, "command": "evil", "risk": "read"}}}))
    rea = mcp_manager.load_config()[0]["rea"]
    assert rea.enabled and rea.risk == "execute" and rea.command == d["rea"].command


async def test_set_enabled_starts_and_stops_one_server_only():
    m = Manager({"fake": ServerConfig(name="fake", command=sys.executable, args=[FAKE], enabled=False),
                 "other": ServerConfig(name="other", command=sys.executable, args=[FAKE])})
    await m.start()
    try:
        other_task = m._servers["other"].task
        assert registry.get("fake_echo") is None and registry.get("other_echo") is not None
        await m.replace(ServerConfig(name="fake", command=sys.executable, args=[FAKE]))
        assert registry.get("fake_echo") is not None
        assert {s.name: s.state for s in m.status()} == {"fake": "ready", "other": "ready"}
        await m.replace(ServerConfig(name="fake", command=sys.executable, args=[FAKE], enabled=False))
        assert registry.get("fake_echo") is None and m._servers["other"].task is other_task
        assert {s.name: s.state for s in m.status()} == {"fake": "disabled", "other": "ready"}
    finally:
        await m.stop()


async def test_mcp_switch_route_writes_the_file(client, monkeypatch):
    seen = []

    class Stub:
        async def replace(self, cfg, wait=None):
            seen.append((cfg.name, cfg.enabled))

        def status(self):
            return []

        async def stop(self):
            pass
    monkeypatch.setattr(mcp_manager, "manager", Stub())
    r = await client.put("/api/mcp/context7", json={"enabled": True})
    assert r.status_code == 200 and seen == [("context7", True)]
    assert json.loads(config.mcp_file().read_text())["servers"]["context7"] == {"enabled": True}
    await client.put("/api/mcp/context7", json={"enabled": False})
    assert json.loads(config.mcp_file().read_text())["servers"]["context7"] == {"enabled": False}
    assert (await client.put("/api/mcp/nope", json={"enabled": True})).status_code == 404
    listed = {s["name"]: s for s in (await client.get("/api/mcp")).json()["configured"]}
    assert listed["context7"]["enabled"] is False and listed["rea"]["risk"] == "execute"
    assert "chunker" in listed and "env" not in listed["rea"]


async def test_mcp_switch_keeps_a_custom_entry_and_refuses_a_broken_file(client, monkeypatch):
    config.mcp_file().write_text(json.dumps({"servers": {"scan": {"command": "scan-mcp", "risk": "network"}}}))
    r = await client.put("/api/mcp/scan", json={"enabled": False})
    assert r.status_code == 200
    assert json.loads(config.mcp_file().read_text())["servers"]["scan"] == {
        "command": "scan-mcp", "risk": "network", "enabled": False}
    config.mcp_file().write_text("{not json")
    assert (await client.put("/api/mcp/rea", json={"enabled": True})).status_code == 400
    assert config.mcp_file().read_text() == "{not json"


async def test_decision_settings(client, monkeypatch):
    from hunterai.tools import runner
    monkeypatch.setattr(runner, "GATE_DEFAULT", True)
    s = (await client.get("/api/settings")).json()
    assert s["decision_gate"] is True and s["decision_provider"] is None and s["decision_model"] is None
    assert s["decision_engine"] is True
    assert (await client.put("/api/settings", json={"decision_engine": False})).json()["decision_engine"] is False
    assert (await client.put("/api/settings", json={"decision_engine": True})).json()["decision_engine"] is True
    s = (await client.put("/api/settings", json={"decision_gate": False, "decision_provider": "ollama",
                                                 "decision_model": "qwen3:8b"})).json()
    assert (s["decision_gate"], s["decision_provider"], s["decision_model"]) == (False, "ollama", "qwen3:8b")
    s = (await client.put("/api/settings", json={"decision_provider": "", "decision_model": ""})).json()
    assert s["decision_provider"] is None and s["decision_model"] is None and s["decision_gate"] is False


async def test_the_switch_keeps_mcp_json_private_and_replaces_serialise(isolated_home):
    import asyncio
    import os
    config.mcp_file().write_text(json.dumps({"servers": {"scan": {"command": "x", "env": {"TOKEN": "t"}}}}))
    os.chmod(config.mcp_file(), 0o600)
    mcp_manager.set_enabled("scan", False)
    assert config.mcp_file().stat().st_mode & 0o777 == 0o600
    m = Manager({"fake": ServerConfig(name="fake", command=sys.executable, args=[FAKE], enabled=False)})
    await m.start()
    try:
        on = ServerConfig(name="fake", command=sys.executable, args=[FAKE])
        await asyncio.gather(m.replace(on), m.replace(on))
        tasks = [t for t in asyncio.all_tasks() if "_own" in repr(t.get_coro())]
        assert len(tasks) == 1 and m.status()[0].state == "ready"
    finally:
        await m.stop()
