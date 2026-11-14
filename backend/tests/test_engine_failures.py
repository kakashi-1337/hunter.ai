"""A run whose database work fails must still end: run_finished published, nothing tracked."""
import asyncio
import sqlite3

import pytest

from hunterai import db, engine, hub
from hunterai.repo import chats, messages, runs
from hunterai.types import TextBlock

from test_engine import Scripted, no_bytes, use


@pytest.fixture
async def started(conn):
    use(abliteration=Scripted("abliteration"))
    chat = await chats.create(conn, None, "pentesting", "abliteration", "m", {})
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="hi")])
    return chat, user


def flaky_factory(fail_on):
    """db.connect, except that the calls whose 1-based number is in fail_on raise."""
    n = 0

    async def factory():
        nonlocal n
        n += 1
        if n in fail_on:
            raise sqlite3.OperationalError("unable to open database file")
        return await db.connect()
    return factory


async def finish(run_id):
    await asyncio.wait_for(engine.wait(run_id), 5)
    await asyncio.sleep(0)  # let the task's done-callback run
    return hub._runs[run_id].events


def assert_forgotten(run_id):
    assert run_id not in engine._tasks and run_id not in engine._started
    assert run_id not in engine._finalizing and run_id not in engine._cancel_requested
    assert engine.is_active(run_id) is False


async def test_open_failure_still_finishes_the_run(conn, started):
    chat, user = started
    run_id, mid = await engine.start_run(flaky_factory({2}), chat.id, user.id, no_bytes)
    evs = await finish(run_id)
    assert evs[-1]["type"] == "run_finished" and evs[-1]["status"] == "error"
    assert "unable to open database file" in evs[-1]["error"]
    assert set(evs[-1]) == {"type", "status", "error", "totals", "message"}
    run, msg = await runs.get(conn, run_id), await messages.get(conn, mid)
    assert run.status == "error" and "unable to open database file" in run.error
    assert msg.status == "error"
    assert_forgotten(run_id)


async def test_database_unreachable_for_the_whole_run_still_publishes_run_finished(conn, started):
    chat, user = started
    run_id, _ = await engine.start_run(flaky_factory(set(range(2, 50))), chat.id, user.id,
                                       no_bytes)
    evs = await finish(run_id)
    assert evs[-1]["type"] == "run_finished" and evs[-1]["status"] == "error"
    assert evs[-1]["totals"] == {} and evs[-1]["message"] is None
    assert (await runs.get(conn, run_id)).status == "running"  # recover() closes it at startup
    assert_forgotten(run_id)


async def test_finalize_write_failure_still_finishes_the_run(conn, started, monkeypatch):
    chat, user = started
    real, calls = runs.finish, []

    async def finish_once_locked(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return await real(*a, **k)
    monkeypatch.setattr(runs, "finish", finish_once_locked)
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    evs = await finish(run_id)
    assert evs[-1]["type"] == "run_finished" and evs[-1]["status"] == "error"
    assert "database is locked" in evs[-1]["error"]
    run, msg = await runs.get(conn, run_id), await messages.get(conn, mid)
    assert run.status == "error" and "database is locked" in run.error and run.ended_at
    assert msg.status == "error"
    assert_forgotten(run_id)


async def test_is_active_tracks_only_live_runs(conn, started):
    use(abliteration=Scripted("abliteration", "slow"))
    chat, user = started
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    assert engine.is_active(run_id) is True
    assert await runs.running_ids(conn, chat.id) == [run_id]
    await finish(run_id)
    assert engine.is_active(run_id) is False and engine.is_active("unknown") is False
    assert await runs.running_ids(conn, chat.id) == []
