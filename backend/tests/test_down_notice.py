"""A service beside HunterAI that answered before and does not answer now is said on the page, not only
in the terminal the start script ran in; and the state of the install can be copied as text for a report."""
import re
from pathlib import Path

import pytest

from hunterai import config, db, diagnostics, envfile, mcp_manager
from hunterai.api import system
from hunterai.repo import settings
from hunterai.tools.base import Tool
from tests.test_api import client  # noqa: F401 (client is a fixture)


def services(monkeypatch, **answers):
    """Which of the services beside HunterAI answer: ollama, qdrant, searxng. The ones not named do not.
    Returns the list of addresses that were asked."""
    asked = []

    async def fetch(url):
        asked.append(url)
        for name, base in (("ollama", system.ollama.host()), ("qdrant", config.qdrant_url()), ("searxng", config.searxng_url())):
            if url.startswith(base):
                return answers.get(name)
        return None
    monkeypatch.setattr(system, "_fetch", fetch)
    return asked


def mcp(monkeypatch, *servers):
    """What the MCP manager says about its servers: (name, state, tools, error) each. None: there is no manager."""
    class Manager:
        def status(self):
            return [mcp_manager.ServerStatus(name=name, transport="stdio", state=state, tools=tools, error=error)
                    for name, state, tools, error in servers]
    monkeypatch.setattr(system.mcp_manager, "get", lambda: None if servers == (None,) else Manager())


def clock(monkeypatch, start=1_000_000.0):
    now = [start]
    monkeypatch.setattr(system.time, "time", lambda: now[0])
    return now


async def item(client, name) -> dict:
    return {i["id"]: i for i in (await client.get("/api/system/checkup")).json()["items"]}[name]


async def stopped(client) -> list[dict]:
    return (await client.get("/api/system/status")).json()["stopped"]


# ---- what stopped

async def test_a_service_that_answered_before_and_does_not_now_has_stopped(client, monkeypatch):
    services(monkeypatch, searxng={}, ollama={"models": []})
    assert (await item(client, "searxng"))["stopped"] is False
    services(monkeypatch, ollama={"models": []})
    down = await item(client, "searxng")
    assert (down["ok"], down["stopped"]) == (False, True) and "scripts/searxng.sh start" in down["fix"]
    assert (await item(client, "ollama"))["stopped"] is False          # it still answers


async def test_a_service_that_was_never_there_is_missing_not_stopped(client, monkeypatch):
    services(monkeypatch)
    for name in ("searxng", "qdrant", "ollama"):
        got = await item(client, name)
        assert (got["ok"], got["stopped"]) == (False, False)


async def test_only_the_services_that_run_beside_hunterai_can_have_stopped(client, monkeypatch):
    """An emptied tool folder or an uninstalled program is the owner's doing, and the list of what is installed says it."""
    services(monkeypatch)
    monkeypatch.setattr(system.toolbox, "installed", lambda: [{"name": "nuclei", "path": "/x/nuclei"}])
    assert (await item(client, "toolbox"))["ok"] is True
    monkeypatch.setattr(system.toolbox, "installed", lambda: [])
    assert (await item(client, "toolbox"))["stopped"] is False


async def test_what_answered_is_remembered_across_a_restart_and_forgotten_after_a_week_without_it(client, monkeypatch):
    now = clock(monkeypatch)
    services(monkeypatch, qdrant={})
    await item(client, "qdrant")
    system.forget(client._transport.app)                               # a new start: nothing is held in memory
    services(monkeypatch)
    assert (await item(client, "qdrant"))["stopped"] is True
    now[0] += 6 * 86400
    assert (await item(client, "qdrant"))["stopped"] is True
    now[0] += 2 * 86400                                                # not seen for over a week: no longer in use
    assert (await item(client, "qdrant"))["stopped"] is False


async def test_the_status_names_what_stopped(client, monkeypatch):
    services(monkeypatch, searxng={}, qdrant={})
    assert await stopped(client) == []
    system.forget(client._transport.app)
    services(monkeypatch, qdrant={})
    assert await stopped(client) == [{"id": "searxng", "label": "SearXNG (web_search)"}]


async def test_the_status_looks_at_the_services_once_a_minute_however_often_it_is_asked(client, monkeypatch):
    now = clock(monkeypatch)
    asked = services(monkeypatch, searxng={})
    for _ in range(5):
        await stopped(client)
        now[0] += 10
    assert len([u for u in asked if u.startswith(config.searxng_url())]) == 1
    now[0] += 20
    await stopped(client)
    assert len([u for u in asked if u.startswith(config.searxng_url())]) == 2


async def test_check_again_in_the_setup_check_is_what_the_status_says_next(client, monkeypatch):
    clock(monkeypatch)
    services(monkeypatch, searxng={})
    assert await stopped(client) == []
    services(monkeypatch)
    assert await stopped(client) == []                                 # looked a moment ago
    await client.get("/api/system/checkup")                            # the owner presses Check again
    assert [s["id"] for s in await stopped(client)] == ["searxng"]


# ---- MCP servers

async def test_an_mcp_server_that_gave_up_is_not_working(client, monkeypatch):
    """It is switched on and does not run: the model works with fewer tools than the owner thinks it has."""
    services(monkeypatch)
    mcp(monkeypatch, ("chunker", "ready", ["read_chunk", "list_chunks"], None), ("rea", "dead", [], "npx was not found"),
        ("context7", "disabled", [], None))
    got = await item(client, "mcp")
    assert (got["ok"], got["stopped"], got["required"]) == (False, True, False)
    assert got["detail"] == "not working: rea (npx was not found); running: chunker"
    assert "Manage MCP servers" in got["fix"]
    assert await stopped(client) == [{"id": "mcp", "label": "MCP server rea"}]


async def test_two_mcp_servers_that_gave_up_are_both_named(client, monkeypatch):
    services(monkeypatch)
    mcp(monkeypatch, ("rea", "dead", [], "npx was not found"), ("burp", "dead", [], None))
    assert (await item(client, "mcp"))["detail"] == "not working: rea (npx was not found), burp"
    assert await stopped(client) == [{"id": "mcp", "label": "MCP servers rea, burp"}]


@pytest.mark.parametrize("state", ["ready", "starting", "restarting", "disabled"])
async def test_an_mcp_server_that_runs_is_starting_or_is_switched_off_is_in_order(client, monkeypatch, state):
    services(monkeypatch)
    mcp(monkeypatch, ("rea", state, [], "the server stopped" if state == "restarting" else None))
    got = await item(client, "mcp")
    assert (got["ok"], got["stopped"], got["fix"]) == (True, False, "")
    assert got["detail"] == {"ready": "running: rea", "starting": "starting: rea", "restarting": "starting: rea",
                             "disabled": "none switched on"}[state]
    assert await stopped(client) == []


async def test_with_the_mcp_servers_switched_off_the_line_says_so(client, monkeypatch):
    services(monkeypatch)
    mcp(monkeypatch, None)
    monkeypatch.setenv("HUNTERAI_MCP", "off")
    got = await item(client, "mcp")
    assert (got["ok"], got["stopped"], got["detail"]) == (True, False, "switched off with HUNTERAI_MCP=off")


# ---- a part that is off on purpose

async def hide(client) -> None:
    r = await client.post("/api/system/stopped/hide")
    assert r.status_code == 200 and r.json() == {"stopped": []}, r.text


async def test_a_part_the_owner_stopped_himself_can_be_kept_out_of_the_notice(client, monkeypatch):
    """Ollama closed to free memory, Docker not started today: said once, not for a week."""
    services(monkeypatch, searxng={}, ollama={"models": []})
    await client.get("/api/system/checkup")
    services(monkeypatch, searxng={})
    await client.get("/api/system/checkup")
    assert [s["id"] for s in await stopped(client)] == ["ollama"]
    await hide(client)
    assert await stopped(client) == []
    system.forget(client._transport.app)                               # and after a restart of HunterAI
    assert await stopped(client) == []
    assert (await item(client, "ollama"))["stopped"] is True           # the setup check keeps saying what is so


async def test_a_hidden_part_that_ran_again_and_stops_again_is_said_again(client, monkeypatch):
    services(monkeypatch, searxng={})
    await client.get("/api/system/checkup")
    services(monkeypatch)
    await client.get("/api/system/checkup")
    await hide(client)
    services(monkeypatch, searxng={})                                  # it is started again
    await client.get("/api/system/checkup")
    services(monkeypatch)                                              # and stops by itself
    await client.get("/api/system/checkup")
    assert [s["id"] for s in await stopped(client)] == ["searxng"]


async def test_hiding_covers_what_is_not_working_now_and_nothing_that_stops_later(client, monkeypatch):
    services(monkeypatch, searxng={}, qdrant={})
    await client.get("/api/system/checkup")
    services(monkeypatch, qdrant={})
    await client.get("/api/system/checkup")
    await hide(client)
    services(monkeypatch)                                              # now Qdrant stops too
    await client.get("/api/system/checkup")
    assert [s["id"] for s in await stopped(client)] == ["qdrant"]


async def test_a_hidden_mcp_server_stays_out_of_the_notice_and_another_one_that_gives_up_is_said(client, monkeypatch):
    services(monkeypatch)
    mcp(monkeypatch, ("rea", "dead", [], "npx was not found"))
    assert await stopped(client) == [{"id": "mcp", "label": "MCP server rea"}]
    await hide(client)
    mcp(monkeypatch, ("rea", "dead", [], "npx was not found"), ("burp", "dead", [], None))
    await client.get("/api/system/checkup")
    assert await stopped(client) == [{"id": "mcp", "label": "MCP server burp"}]
    mcp(monkeypatch, ("rea", "ready", ["decompile"], None), ("burp", "dead", [], None))      # rea was switched off and on
    await client.get("/api/system/checkup")
    mcp(monkeypatch, ("rea", "dead", [], "it crashed"), ("burp", "dead", [], None))
    await client.get("/api/system/checkup")
    assert await stopped(client) == [{"id": "mcp", "label": "MCP servers rea, burp"}]


async def test_what_is_hidden_is_a_list_the_status_survives_when_it_cannot_read_it(client, monkeypatch):
    services(monkeypatch, searxng={})
    await client.get("/api/system/checkup")
    conn = await db.connect()
    try:
        await settings.put(conn, "stopped:hidden", 5)                  # not a list: written by hand
    finally:
        await conn.close()
    services(monkeypatch)
    await client.get("/api/system/checkup")
    assert [s["id"] for s in await stopped(client)] == ["searxng"]


# ---- diagnostics

async def text(client) -> str:
    r = await client.get("/api/system/diagnostics")
    assert r.status_code == 200
    return r.json()["text"]


async def test_the_diagnostics_say_what_runs_and_what_does_not(client, monkeypatch):
    services(monkeypatch, searxng={}, ollama={"models": [{"name": "qwen3:8b"}]})
    await client.get("/api/system/checkup")
    services(monkeypatch, ollama={"models": [{"name": "qwen3:8b"}]})
    got = await text(client)
    assert got.startswith("HunterAI diagnostics, ")
    assert "\nRestart needed: no\n" in got
    assert "  ok       Ollama (local models, the fallback, look_at_image): answers at " in got
    assert "  STOPPED  SearXNG (web_search): no answer at " in got
    assert "  missing  Qdrant (the knowledge base): no answer at " in got
    assert "Python " in got and "Server started " in got


async def test_the_diagnostics_name_the_version_of_the_code_and_how_much_of_it_is_the_owners(client, monkeypatch):
    services(monkeypatch)
    monkeypatch.setattr(diagnostics, "_git", lambda *args: {
        ("rev-parse", "--short", "HEAD"): "d65353d", ("log", "-1", "--format=%cs"): "2026-11-17",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("status", "--porcelain"): " M backend/hunterai/db.py\n M backend/hunterai/modes.py\n?? backend/hunterai/phantomstrike.py",
    }.get(args))
    assert "\nVersion: d65353d (2026-11-17) on main; 2 file(s) changed here, 1 file(s) of the owner's own\n" in await text(client)
    monkeypatch.setattr(diagnostics, "_git", lambda *args: None)
    assert "\nVersion: unknown (this folder is not a git checkout)\n" in await text(client)


async def test_the_diagnostics_hold_no_key_no_proxy_address_and_nothing_of_the_work(client, monkeypatch):
    """They are made to be pasted into a report."""
    services(monkeypatch)
    envfile.set_var(config.env_file(), "ABLIT_API_KEY", "sk-secret-12345")
    saved = await client.put("/api/settings", json={"upstream_proxy": "http://me:long-password@proxy.example:8080"})
    assert saved.status_code == 200, saved.text
    target = await client.post("/api/targets", json={"name": "acme-private-program", "notes": "shop.acme.example pays for IDOR"})
    assert target.status_code == 200, target.text
    got = await text(client)
    for secret in ("sk-secret-12345", "long-password", "proxy.example", "acme", "me:"):
        assert secret not in got
    assert "ABLIT_API_KEY: set" in got and "Proxy for targets: set" in got and "Proxy for model providers: none" in got


async def test_the_diagnostics_name_the_models_in_use_and_the_window_of_local_ones(client, monkeypatch):
    services(monkeypatch)
    await client.put("/api/settings", json={"local_window": 32768})
    got = await text(client)
    assert "Default model: " in got and "Fallback model: " in got and "Context window for local models: 32768" in got


async def test_the_diagnostics_list_the_mcp_servers_and_count_the_tools(client, monkeypatch):
    """How many tools go into a request is the first thing to know when one does not fit a model's window."""
    services(monkeypatch)
    mcp(monkeypatch, ("chunker", "ready", ["read_chunk", "list_chunks"], None), ("rea", "dead", [], "npx was not found"),
        ("context7", "disabled", [], None))
    got = await text(client)
    assert "\nMCP servers:\n  ready    chunker: 2 tools\n  dead     rea: npx was not found\n  disabled context7\n" in got
    assert "  STOPPED  MCP servers (tools from other programs): not working: rea (npx was not found); running: chunker\n" in got
    own = len([t for t in system.tools.all() if t.source == "native"])
    assert own > 10 and f"\nTools: {own} of HunterAI's own, 0 from MCP servers\n" in got
    mcp(monkeypatch, None)
    assert "\nMCP servers: switched off\n" in await text(client)

    async def handler(args, ctx):
        return None
    for name in ("read_chunk", "list_chunks"):
        system.tools.register(Tool(name, "d", {"type": "object"}, "read", handler, source="mcp:chunker"))
    try:
        assert f"\nTools: {own} of HunterAI's own, 2 from MCP servers\n" in await text(client)
    finally:
        system.tools.unregister_source("mcp:chunker")


async def test_the_diagnostics_name_no_home_folder_and_no_login_inside_an_address(client, monkeypatch):
    """An error of an MCP server is its own text: it can hold the path of a program or the address it was given."""
    services(monkeypatch)
    mcp(monkeypatch, ("burp", "dead", [], f"{Path.home()}/tools/burp-mcp was not found\nTraceback ..."),
        ("jev", "dead", [], "no answer from https://dave:hunter2@mcp.jev.example/sse?api_key=abc123def after 3 tries"))
    got = await text(client)
    assert "  dead     burp: ~/tools/burp-mcp was not found\n" in got
    assert "  dead     jev: no answer from https://mcp.jev.example/sse?... after 3 tries\n" in got
    for secret in (str(Path.home()), "hunter2", "dave:", "abc123def", "Traceback"):
        assert secret not in got


def test_the_diagnostics_are_lines_a_person_can_read(monkeypatch):
    monkeypatch.setattr(diagnostics, "_git", lambda *args: None)
    got = diagnostics.text({"items": []}, {"restart_needed": True, "local_models": [{"name": "qwen3:8b", "vram_mb": 5120}]},
                           {"machine": {"cores": 12, "memory_gb": 31.2}}, None, [], now=diagnostics.STARTED + 125)
    assert re.search(r"\nServer started \d{4}-\d\d-\d\d \d\d:\d\d, running 2 min\n", got)
    assert "\nRestart needed: yes: the code on disk changed after the server started\n" in got
    assert ", 12 cores, 31.2 GB\n" in got and "\nLocal models loaded: qwen3:8b (5120 MB on the GPU)\n" in got
    assert got.endswith("\n") and "\n\n" not in got


@pytest.mark.parametrize("seconds,said", [(42, "42 s"), (125, "2 min"), (3 * 3600 + 120, "3 h 2 min"), (2 * 86400 + 3600, "2 d 1 h")])
def test_how_long_the_server_has_run_is_said_in_plain_units(seconds, said):
    assert diagnostics.lasted(seconds) == said


async def test_what_the_store_remembers_is_a_time(client, monkeypatch):
    clock(monkeypatch, 1_700_000_000.0)
    services(monkeypatch, searxng={})
    await client.get("/api/system/checkup")
    conn = await db.connect()
    try:
        assert await settings.get(conn, "answered:searxng") == 1_700_000_000.0
        assert await settings.get(conn, "answered:qdrant") is None
    finally:
        await conn.close()


async def test_a_time_the_store_cannot_read_counts_as_never(client, monkeypatch):
    """The status is asked every few seconds: nothing kept in the store may make it fail."""
    conn = await db.connect()
    try:
        await settings.put(conn, "answered:searxng", "soon")
        await settings.put(conn, "answered:qdrant", {"at": 5})
    finally:
        await conn.close()
    services(monkeypatch)
    r = await client.get("/api/system/status")
    assert r.status_code == 200 and r.json()["stopped"] == []
