from hunterai import caido, config, db, thermal
from hunterai.api import system
from hunterai.repo import findings
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)


def answers(monkeypatch, table: dict):
    """What each local service answers: a url prefix -> its JSON, or None for no answer."""
    async def fetch(url: str):
        for prefix, body in table.items():
            if url.startswith(prefix):
                return body
        return None
    monkeypatch.setattr(system, "_fetch", fetch)


async def test_the_machine_state_names_the_temperature_and_the_loaded_local_models(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "dist_dir", lambda: tmp_path)          # no interface is built there
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 71.0)
    monkeypatch.delenv("HUNTERAI_MAX_CPU_TEMP", raising=False)
    answers(monkeypatch, {"http://127.0.0.1:11434/api/ps": {"models": [{"name": "mimo:9b", "size": 9 * 2**30, "size_vram": 9 * 2**30}]}})
    assert (await client.get("/api/system/status")).json() == {
        "cpu_temp": 71.0, "cpu_limit": 90.0, "gpu_temp": None, "gpu_limit": 85.0,
        "local_models": [{"name": "mimo:9b", "vram_mb": 9216, "size_mb": 9216, "processor": "100% GPU", "context": None}], "colibri": None,
        "restart_needed": False, "stopped": [], "page": None}
    answers(monkeypatch, {})                      # Ollama does not answer: unknown, which is not "none loaded"
    monkeypatch.setattr(thermal, "cpu_temp", lambda: None)
    assert (await client.get("/api/system/status")).json() == {
        "cpu_temp": None, "cpu_limit": 90.0, "gpu_temp": None, "gpu_limit": 85.0, "local_models": None, "colibri": None,
        "restart_needed": False, "stopped": [], "page": None}


async def test_the_graphics_card_is_asked_only_while_a_local_model_is_loaded(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "dist_dir", lambda: tmp_path)
    monkeypatch.setenv("HUNTERAI_MAX_GPU_TEMP", "80")
    asked = []

    async def card():
        asked.append(1)
        return 64.0
    monkeypatch.setattr(thermal, "gpu_temp", card)
    loaded = {"models": [{"name": "mimo:9b", "size": 9 * 2**30, "size_vram": 9 * 2**30}]}
    answers(monkeypatch, {"http://127.0.0.1:11434/api/ps": loaded})
    said = (await client.get("/api/system/status")).json()
    assert (said["gpu_temp"], said["gpu_limit"]) == (64.0, 80.0) and asked == [1]
    # Asking wakes a card that a laptop has put to sleep: with no model in memory it is left alone.
    for ps in ({"models": []}, None):
        answers(monkeypatch, {"http://127.0.0.1:11434/api/ps": ps} if ps else {})
        said = (await client.get("/api/system/status")).json()
        assert (said["gpu_temp"], said["gpu_limit"]) == (None, 80.0)
    assert asked == [1]

    async def held():
        return {"model": "qwen3.6-27b", "context": 16384}
    monkeypatch.setattr(system, "_held_by_colibri", held)                     # colibri holds one: Ollama does not list it
    assert (await client.get("/api/system/status")).json()["gpu_temp"] == 64.0 and asked == [1, 1]


async def test_the_checkup_says_what_is_missing_and_how_to_get_it(client, monkeypatch):
    monkeypatch.setattr(system, "_browser_installed", lambda: False)
    monkeypatch.setattr("hunterai.providers.codex.shutil.which", lambda *a, **k: None)   # no Codex CLI on this machine
    monkeypatch.delenv("HUNTERAI_MOCK")          # the test client's stand-in model is not a model to talk to
    answers(monkeypatch, {})
    got = (await client.get("/api/system/checkup")).json()
    items = {i["id"]: i for i in got["items"]}
    assert got["ready"] is False
    assert set(items) == {"model", "ollama", "embedder", "qdrant", "searxng", "caido", "browser", "toolbox", "rea", "mcp"}
    assert items["model"]["required"] and not items["model"]["ok"] and ".env" in items["model"]["fix"]
    assert all(not i["ok"] and i["fix"] for k, i in items.items() if k != "mcp")
    assert items["mcp"]["ok"] and not items["mcp"]["fix"]       # no MCP server is switched on and not running
    assert [i["id"] for i in got["items"] if i["required"]] == ["model"]

    # A key in .env is enough to chat, whatever else is missing.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    got = (await client.get("/api/system/checkup")).json()
    assert got["ready"] is True and "anthropic" in {i["id"]: i for i in got["items"]}["model"]["detail"]

    # No key, but Ollama with a chat model: also enough. An embedder alone is not.
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(system, "_browser_installed", lambda: True)
    answers(monkeypatch, {"http://127.0.0.1:11434/api/tags": {"models": [{"name": "bge-m3:latest"}]},
                          "http://127.0.0.1:6333": {}, "http://127.0.0.1:8888": {}})
    got = (await client.get("/api/system/checkup")).json()
    items = {i["id"]: i for i in got["items"]}
    assert got["ready"] is False
    assert [k for k, i in items.items() if i["ok"]] == ["ollama", "embedder", "qdrant", "searxng", "browser", "mcp"]
    answers(monkeypatch, {"http://127.0.0.1:11434/api/tags": {"models": [{"name": "bge-m3:latest"}, {"name": "qwen3:8b"}]}})
    got = (await client.get("/api/system/checkup")).json()
    assert got["ready"] is True and "qwen3:8b" in {i["id"]: i for i in got["items"]}["model"]["detail"]


async def test_caido_routes_show_what_was_recorded_without_starting_it(client, monkeypatch):
    assert (await client.get("/api/caido/status")).json() == {"available": False, "running": False}
    assert (await client.get("/api/caido/requests")).status_code == 409

    started = []
    monkeypatch.setattr(caido, "available", lambda: True)
    monkeypatch.setattr(system, "_caido_up", lambda: _async(False))
    assert (await client.get("/api/caido/status")).json() == {"available": True, "running": False, "url": caido.base_url()}
    assert (await client.get("/api/caido/requests")).status_code == 409
    assert started == []

    async def status():
        return {"url": "http://127.0.0.1:8791", "version": "0.59.0", "project": "hunterai", "temporary": True,
                "requests": 2, "managed": True}

    async def history(filter_code, limit, offset):
        started.append((filter_code, limit, offset))
        return [{"id": "7", "host": "example.com", "port": 443, "method": "GET", "path": "/a", "query": "x=1", "isTls": True,
                 "source": "PROXY", "createdAt": 1731500000000, "raw": b"GET /a?x=1 HTTP/1.1\r\n\r\n",
                 "response": {"id": "9", "statusCode": 200, "length": 12, "roundtripTime": 40}}], 1

    async def exchange(rid):
        if rid != "7":
            return None
        return {"id": "7", "host": "example.com", "port": 443, "isTls": True, "raw": b"GET /a HTTP/1.1\r\nHost: example.com\r\n\r\n",
                "response": {"statusCode": 200, "raw": b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nhello"}}

    monkeypatch.setattr(system, "_caido_up", lambda: _async(True))
    monkeypatch.setattr(caido, "status", status)
    monkeypatch.setattr(caido, "history", history)
    monkeypatch.setattr(caido, "exchange", exchange)
    s = (await client.get("/api/caido/status")).json()
    assert (s["available"], s["running"], s["requests"], s["temporary"]) == (True, True, 2, True)
    got = (await client.get("/api/caido/requests", params={"filter": 'req.host.eq:"example.com"', "limit": 10})).json()
    assert started == [('req.host.eq:"example.com"', 10, 0)]
    assert got == {"total": 1, "rows": [{"id": "7", "method": "GET", "url": "https://example.com/a?x=1", "status": 200,
                                         "length": 12, "ms": 40, "source": "PROXY", "at": 1731500000000}], "left_out": 0}
    one = (await client.get("/api/caido/requests/7")).json()
    assert one["request"].startswith("GET /a HTTP/1.1") and one["response"].endswith("hello")
    assert (await client.get("/api/caido/requests/8")).status_code == 404

    async def broken(*a):
        raise caido.CaidoError("the filter does not parse")
    monkeypatch.setattr(caido, "history", broken)
    r = await client.get("/api/caido/requests", params={"filter": "nonsense"})
    assert r.status_code == 400 and "does not parse" in r.json()["detail"]


async def _async(value):
    return value


async def test_every_finding_is_listed_with_its_target_and_chat(client):
    a = (await client.post("/api/targets", json={"name": "acme"})).json()
    b = (await client.post("/api/targets", json={"name": "bolt"})).json()
    chat = await new_chat(client, target_id=a["id"])
    conn = await db.connect()
    try:
        await findings.add(conn, a["id"], "IDOR", "high", "proof", chat_id=chat["id"])
        await findings.add(conn, b["id"], "Open redirect", "low", "")
        await findings.add(conn, a["id"], "Banner", "info", "", chat_id="gone")
    finally:
        await conn.close()
    got = (await client.get("/api/findings")).json()
    assert [(f["title"], f["target_name"], f["chat_title"]) for f in got] == [
        ("Banner", "acme", None), ("Open redirect", "bolt", None), ("IDOR", "acme", chat["title"])]
    assert got[2]["chat_id"] == chat["id"] and got[2]["evidence"] == "proof" and got[2]["triage"] == {}
