"""Concurrent sends/edits/retries to one chat: exactly one may start a run."""
import asyncio
import json
import sqlite3

import httpx
import pytest

from hunterai import config, engine, hub
from hunterai.api.app import create_app
from hunterai.repo import runs as runs_repo

BASE = "http://127.0.0.1:8787"


@pytest.fixture
async def client(monkeypatch, isolated_home):
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            yield c


async def new_chat(client):
    r = await client.post("/api/chats", json={"provider": "mock", "model": "mock-1"})
    assert r.status_code == 200, r.text
    return r.json()


async def events(client, run_id):
    r = await client.get(f"/api/runs/{run_id}/events")
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


async def send_and_wait(client, chat_id, text):
    r = await client.post(f"/api/chats/{chat_id}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    await events(client, r.json()["run_id"])
    return r.json()


def counts(chat_id):
    db = sqlite3.connect(config.db_path())
    try:
        return (db.execute("select count(*) from messages where chat_id=?", (chat_id,)).fetchone()[0],
                db.execute("select count(*) from runs where chat_id=?", (chat_id,)).fetchone()[0])
    finally:
        db.close()


async def race(client, chat_id, calls):
    rs = await asyncio.gather(*calls)
    for r in rs:
        if r.status_code == 200:
            await events(client, r.json()["run_id"])
    return sorted(r.status_code for r in rs)


async def test_concurrent_sends_start_exactly_one_run(client):
    chat = await new_chat(client)
    codes = await race(client, chat["id"], [
        client.post(f"/api/chats/{chat['id']}/messages", json={"text": "slow"})
        for i in range(3)])
    assert codes == [200, 409, 409]
    assert counts(chat["id"]) == (2, 1)  # one user + one assistant message, one run


async def test_concurrent_edits_start_exactly_one_run(client):
    chat = await new_chat(client)
    first = await send_and_wait(client, chat["id"], "hi")
    codes = await race(client, chat["id"], [
        client.post(f"/api/messages/{first['user_message_id']}/edit", json={"text": "slow"})
        for i in range(3)])
    assert codes == [200, 409, 409]
    assert counts(chat["id"]) == (4, 2)


async def test_concurrent_retries_start_exactly_one_run(client):
    chat = await new_chat(client)
    first = await send_and_wait(client, chat["id"], "slow")
    codes = await race(client, chat["id"], [
        client.post(f"/api/messages/{first['assistant_message_id']}/retry") for _ in range(3)])
    assert codes == [200, 409, 409]
    assert counts(chat["id"]) == (3, 2)


async def test_chat_is_usable_again_when_the_run_could_not_record_its_end(client, monkeypatch):
    """Every database write at the end of the run fails: the row stays 'running', but the
    engine no longer tracks the run, so the chat must not answer 409 until a restart."""
    async def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    chat = await new_chat(client)
    real_finish = runs_repo.finish
    monkeypatch.setattr(runs_repo, "finish", locked)
    r = await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "hi"})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    await asyncio.wait_for(engine.wait(run_id), 5)
    await asyncio.sleep(0)
    last = hub._runs[run_id].events[-1]
    assert last["type"] == "run_finished" and last["status"] == "error"
    assert "database is locked" in last["error"]
    assert (await client.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    monkeypatch.setattr(runs_repo, "finish", real_finish)
    again = await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "again"})
    assert again.status_code == 200, again.text
    await events(client, again.json()["run_id"])
