import asyncio
import json

import httpx
import pytest

from hunterai import db, engine
from hunterai.api.app import create_app
from hunterai.repo import chats as chats_repo
from hunterai.repo import messages as messages_repo
from hunterai.repo import runs as runs_repo
from hunterai.types import TextBlock

BASE = "http://127.0.0.1:8787"


@pytest.fixture
async def client(monkeypatch, isolated_home):
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            yield c


async def new_chat(client, **kw):
    r = await client.post("/api/chats", json={"provider": "mock", "model": "mock-1", **kw})
    assert r.status_code == 200, r.text
    return r.json()


async def events(client, run_id):
    r = await client.get(f"/api/runs/{run_id}/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


async def send(client, chat_id, text, upload_ids=None, wait=True):
    r = await client.post(f"/api/chats/{chat_id}/messages",
                          json={"text": text, "upload_ids": upload_ids or []})
    assert r.status_code == 200, r.text
    out = r.json()
    if wait:
        out["events"] = await events(client, out["run_id"])
    return out


def texts(chat_json):
    return [next((b["text"] for b in m["blocks"] if b["type"] == "text"), "")
            for m in chat_json["messages"]]


async def test_send_stream_and_reload(client):
    chat = await new_chat(client)
    out = await send(client, chat["id"], "hi")
    assert out["events"][0]["type"] == "run_started" and out["events"][-1]["type"] == "run_finished"
    got = (await client.get(f"/api/chats/{chat['id']}")).json()
    assert texts(got) == ["hi", "echo: hi"]
    assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
    assert got["messages"][1]["id"] == out["assistant_message_id"]
    assert got["messages"][0]["siblings"] == [out["user_message_id"]]
    assert got["chat"]["title"] == "hi"


async def test_chat_defaults(client, monkeypatch):
    r = (await client.post("/api/chats", json={})).json()
    assert (r["provider"], r["model"], r["mode"], r["target_id"]) == \
        ("abliteration", "abliterated-model-large-v2", "pentesting", None)
    monkeypatch.setenv("ABLIT_MODEL", "abliterated-model-large-flash")
    assert (await client.post("/api/chats", json={})).json()["model"] == "abliterated-model-large-flash"
    await client.put("/api/settings", json={"default_provider": "mock", "default_model": "mock-1"})
    r = (await client.post("/api/chats", json={})).json()
    assert (r["provider"], r["model"]) == ("mock", "mock-1")


async def test_chat_validation(client):
    assert (await client.post("/api/chats", json={"mode": "yolo"})).status_code == 422
    assert (await client.post("/api/chats", json={"target_id": "missing"})).status_code == 404
    assert (await client.get("/api/chats/missing")).status_code == 404
    assert (await client.patch("/api/chats/missing", json={"title": "x"})).status_code == 404
    chat = await new_chat(client)
    assert (await client.post(f"/api/chats/{chat['id']}/messages",
                              json={"text": "   ", "upload_ids": []})).status_code == 400
    assert (await client.post(f"/api/chats/{chat['id']}/messages",
                              json={"text": "x", "upload_ids": ["nope"]})).status_code == 400
    assert (await client.post("/api/chats/missing/messages",
                              json={"text": "x"})).status_code == 404


async def test_patch_and_list_and_delete_chat(client):
    t = (await client.post("/api/targets", json={"name": "acme", "scope": {"in": ["a.test"]}})).json()
    chat = await new_chat(client)
    r = await client.patch(f"/api/chats/{chat['id']}", json={
        "title": "renamed", "mode": "coding", "target_id": t["id"], "options": {"effort": "high"}})
    assert r.status_code == 200
    j = r.json()
    assert (j["title"], j["mode"], j["target_id"], j["options"]) == \
        ("renamed", "coding", t["id"], {"effort": "high"})
    assert [c["id"] for c in (await client.get("/api/chats", params={"target_id": t["id"]})).json()] \
        == [chat["id"]]
    assert (await client.patch(f"/api/chats/{chat['id']}", json={"target_id": None})).json()["target_id"] is None
    assert (await client.delete(f"/api/chats/{chat['id']}")).status_code == 204
    assert (await client.get(f"/api/chats/{chat['id']}")).status_code == 404
    assert (await client.get("/api/chats")).json() == []


async def test_targets_crud(client):
    t = (await client.post("/api/targets", json={"name": "acme"})).json()
    assert t["scope"] == {"in": [], "out": []}
    assert (await client.post("/api/targets", json={"name": "  "})).status_code == 422
    r = await client.patch(f"/api/targets/{t['id']}", json={"notes": "n", "scope": {"in": ["x"], "out": []}})
    assert r.json()["notes"] == "n" and r.json()["scope"]["in"] == ["x"]
    assert len((await client.get("/api/targets")).json()) == 1
    assert (await client.delete(f"/api/targets/{t['id']}")).status_code == 204
    assert (await client.patch(f"/api/targets/{t['id']}", json={"notes": "n"})).status_code == 404


async def test_edit_branches_and_switch_back(client):
    chat = await new_chat(client)
    first = await send(client, chat["id"], "one")
    r = await client.post(f"/api/messages/{first['user_message_id']}/edit", json={"text": "two"})
    assert r.status_code == 200
    await events(client, r.json()["run_id"])
    got = (await client.get(f"/api/chats/{chat['id']}")).json()
    assert texts(got) == ["two", "echo: two"]
    assert got["messages"][0]["siblings"] == [first["user_message_id"], r.json()["user_message_id"]]
    r2 = await client.post(f"/api/chats/{chat['id']}/active-leaf",
                           json={"message_id": first["user_message_id"]})
    assert r2.status_code == 200
    assert texts((await client.get(f"/api/chats/{chat['id']}")).json()) == ["one", "echo: one"]


async def test_edit_mid_thread_keeps_earlier_messages(client):
    chat = await new_chat(client)
    await send(client, chat["id"], "one")
    second = await send(client, chat["id"], "two")
    r = await client.post(f"/api/messages/{second['user_message_id']}/edit", json={"text": "TWO"})
    await events(client, r.json()["run_id"])
    assert texts((await client.get(f"/api/chats/{chat['id']}")).json()) == \
        ["one", "echo: one", "TWO", "echo: TWO"]


async def test_retry_creates_assistant_sibling(client):
    chat = await new_chat(client)
    first = await send(client, chat["id"], "hi")
    r = await client.post(f"/api/messages/{first['assistant_message_id']}/retry")
    assert r.status_code == 200 and r.json()["user_message_id"] == first["user_message_id"]
    await events(client, r.json()["run_id"])
    got = (await client.get(f"/api/chats/{chat['id']}")).json()
    assert len(got["messages"]) == 2
    assert got["messages"][1]["siblings"] == [first["assistant_message_id"],
                                              r.json()["assistant_message_id"]]


async def test_edit_and_retry_role_rules(client):
    chat = await new_chat(client)
    first = await send(client, chat["id"], "hi")
    assert (await client.post(f"/api/messages/{first['assistant_message_id']}/edit",
                              json={"text": "x"})).status_code == 400
    assert (await client.post(f"/api/messages/{first['user_message_id']}/retry")).status_code == 400
    assert (await client.post("/api/messages/missing/retry")).status_code == 404
    assert (await client.post("/api/messages/missing/edit", json={"text": "x"})).status_code == 404
    assert (await client.post(f"/api/messages/{first['user_message_id']}/edit",
                              json={"text": ""})).status_code == 400


async def test_delete_message(client):
    chat = await new_chat(client)
    first = await send(client, chat["id"], "hi")
    assert (await client.delete(f"/api/messages/{first['assistant_message_id']}")).status_code == 204
    assert texts((await client.get(f"/api/chats/{chat['id']}")).json()) == ["hi"]
    assert (await client.delete("/api/messages/missing")).status_code == 404


async def test_run_detail_totals_and_listing(client):
    chat = await new_chat(client)
    out = await send(client, chat["id"], "hi")
    run = (await client.get(f"/api/runs/{out['run_id']}")).json()
    assert run["status"] == "done" and run["totals"]["turns"] == 1 and run["totals"]["calls"] == 0
    assert run["steps"][0]["kind"] == "model_call" and run["steps"][0]["name"] == "mock/mock-1"
    lst = (await client.get(f"/api/chats/{chat['id']}/runs")).json()
    assert [r["id"] for r in lst] == [out["run_id"]]
    assert (await client.get("/api/runs/missing")).status_code == 404
    assert (await client.get("/api/runs/missing/events")).status_code == 404


async def test_conflict_when_run_in_progress(client):
    chat = await new_chat(client)
    out = await send(client, chat["id"], "slow", wait=False)
    r = await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "again"})
    assert r.status_code == 409
    assert (await client.delete(f"/api/messages/{out['user_message_id']}")).status_code == 409
    await events(client, out["run_id"])
    assert (await client.post(f"/api/chats/{chat['id']}/messages",
                              json={"text": "again"})).status_code == 200


async def test_cancel_endpoint(client):
    chat = await new_chat(client)
    out = await send(client, chat["id"], "slow", wait=False)
    await asyncio.sleep(0.08)
    assert (await client.post(f"/api/runs/{out['run_id']}/cancel")).json() == {"cancelled": True}
    evs = await events(client, out["run_id"])
    assert evs[-1]["status"] == "cancelled"
    assert (await client.post(f"/api/runs/{out['run_id']}/cancel")).json() == {"cancelled": False}


async def test_events_for_finished_run_after_hub_forgot_it(client):
    from hunterai import hub
    chat = await new_chat(client)
    out = await send(client, chat["id"], "hi")
    hub.reset()
    evs = await events(client, out["run_id"])
    assert [e["type"] for e in evs] == ["run_started", "run_finished"]
    assert evs[-1]["status"] == "done" and evs[-1]["message"]["id"] == out["assistant_message_id"]


async def test_error_run_is_reported(client):
    chat = await new_chat(client)
    out = await send(client, chat["id"], "fail:credits")
    assert out["events"][-1]["status"] == "error" and out["events"][-1]["error"]
    got = (await client.get(f"/api/chats/{chat['id']}")).json()
    assert got["messages"][1]["status"] == "error"


async def test_search_endpoint(client):
    chat = await new_chat(client)
    await send(client, chat["id"], "find the sqli payload")
    hits = (await client.get("/api/search", params={"q": "sqli"})).json()
    assert hits and hits[0]["chat_id"] == chat["id"]
    for q in ['"a AND', "*", "", "a:b", "-"]:
        r = await client.get("/api/search", params={"q": q})
        assert r.status_code == 200 and isinstance(r.json(), list)


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "attacker.test:8787"])
async def test_host_header_rejected(client, host):
    r = await client.get("/api/chats", headers={"Host": host})
    assert r.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8787", "localhost:8787", "localhost:5173", "127.0.0.1"])
async def test_local_hosts_allowed(client, host):
    assert (await client.get("/api/chats", headers={"Host": host})).status_code == 200


async def test_cross_site_origin_rejected(client):
    r = await client.post("/api/chats", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.post("/api/chats", json={"provider": "mock", "model": "mock-1"},
                          headers={"Origin": "http://localhost:8790"})
    assert r.status_code == 200


async def test_startup_recovers_interrupted(isolated_home, monkeypatch):
    conn = await db.connect()
    await db.migrate(conn)
    chat = await chats_repo.create(conn, None, "plan", "mock", "mock-1", {})
    u = await messages_repo.add(conn, chat.id, None, "user", [TextBlock(text="q")])
    a = await messages_repo.add(conn, chat.id, u.id, "assistant", [TextBlock(text="par")],
                                status="streaming")
    run = await runs_repo.create(conn, chat.id, u.id, a.id)
    await conn.close()
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            assert (await c.get(f"/api/runs/{run.id}")).json()["status"] == "interrupted"
            got = (await c.get(f"/api/chats/{chat.id}")).json()
            assert got["messages"][1]["status"] == "interrupted"
            assert texts(got) == ["q", "par"]
            assert (await c.post(f"/api/messages/{a.id}/retry")).status_code == 200


async def test_upload_roundtrip_and_attach(client):
    chat = await new_chat(client)
    r = await client.post("/api/uploads", files={"file": ("notes.txt", b"token=abc", "text/plain")},
                          data={"chat_id": chat["id"]})
    assert r.status_code == 200, r.text
    up = r.json()
    assert up["filename"] == "notes.txt" and "path" not in up
    out = await send(client, chat["id"], "read this", [up["id"]])
    got = (await client.get(f"/api/chats/{chat['id']}")).json()
    blocks = got["messages"][0]["blocks"]
    assert [b["type"] for b in blocks] == ["text", "file"] and blocks[1]["text"] == "token=abc"
    assert "token=abc" in texts(got)[1]
    dl = await client.get(f"/api/uploads/{up['id']}")
    assert dl.content == b"token=abc" and "attachment" in dl.headers["content-disposition"]
    assert dl.headers["x-content-type-options"] == "nosniff"
    assert (await client.get("/api/uploads/missing")).status_code == 404
    assert out["events"][-1]["status"] == "done"


async def test_upload_only_message_and_image_inline(client):
    chat = await new_chat(client)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    up = (await client.post("/api/uploads", files={"file": ("s.png", png, "image/png")})).json()
    r = await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "", "upload_ids": [up["id"]]})
    assert r.status_code == 200
    await events(client, r.json()["run_id"])
    dl = await client.get(f"/api/uploads/{up['id']}")
    assert dl.headers["content-type"] == "image/png" and "inline" in dl.headers["content-disposition"]


async def test_html_upload_is_never_served_as_html(client):
    up = (await client.post("/api/uploads", files={
        "file": ("x.html", b"<script>alert(1)</script>", "text/html")})).json()
    dl = await client.get(f"/api/uploads/{up['id']}")
    assert "attachment" in dl.headers["content-disposition"]
    assert not dl.headers["content-type"].startswith("text/html")


async def test_upload_too_large(client, monkeypatch):
    from hunterai import uploads
    monkeypatch.setattr(uploads, "MAX_BYTES", 4)
    r = await client.post("/api/uploads", files={"file": ("a", b"12345", "text/plain")})
    assert r.status_code == 413


async def test_edit_keeps_attachments_unless_replaced(client):
    chat = await new_chat(client)
    up = (await client.post("/api/uploads", files={"file": ("a.txt", b"AAA", "text/plain")})).json()
    first = await send(client, chat["id"], "q", [up["id"]])
    r = await client.post(f"/api/messages/{first['user_message_id']}/edit", json={"text": "q2"})
    await events(client, r.json()["run_id"])
    got = (await client.get(f"/api/chats/{chat['id']}")).json()
    assert [b["type"] for b in got["messages"][0]["blocks"]] == ["text", "file"]
    r = await client.post(f"/api/messages/{r.json()['user_message_id']}/edit",
                          json={"text": "q3", "upload_ids": []})
    await events(client, r.json()["run_id"])
    got = (await client.get(f"/api/chats/{chat['id']}")).json()
    assert [b["type"] for b in got["messages"][0]["blocks"]] == ["text"]
