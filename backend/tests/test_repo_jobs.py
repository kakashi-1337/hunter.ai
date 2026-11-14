import asyncio

import pytest

from hunterai import db
from hunterai.repo import jobs


async def force(conn, job_id, state, **cols):
    sets = ", ".join(["state=?"] + [f"{k}=?" for k in cols])
    await conn.execute(f"update jobs set {sets} where id=?", (state, *cols.values(), job_id))
    await conn.commit()


async def test_claim_is_atomic(conn):
    j = await jobs.enqueue(conn, "ingest", {}); c2 = await db.connect()
    got = await asyncio.gather(jobs.claim(conn, ["ingest"]), jobs.claim(c2, ["ingest"])); await c2.close()
    j2 = await jobs.get(conn, j.id)
    assert [g.id for g in got if g] == [j.id] and (j2.state, j2.attempts) == ("running", 1) and j2.started_at


async def test_claim_takes_oldest_queued_of_wanted_kind(conn):
    other = await jobs.enqueue(conn, "mcp_install", {})
    a = await jobs.enqueue(conn, "ingest", {"n": 1}); b = await jobs.enqueue(conn, "ingest", {"n": 2})
    got = [await jobs.claim(conn, ["ingest"]) for _ in range(3)]
    assert [g.id if g else None for g in got] == [a.id, b.id, None]
    assert (await jobs.get(conn, other.id)).state == "queued"


@pytest.mark.parametrize("action,start,end", [("pause", "queued", "paused"), ("pause", "running", "paused"), ("resume", "paused", "queued"),
    ("cancel", "queued", "cancelled"), ("cancel", "running", "cancelled"), ("cancel", "paused", "cancelled"), ("cancel", "failed", "cancelled"), ("retry", "failed", "queued")])
async def test_request_transitions(conn, action, start, end):
    j = await jobs.enqueue(conn, "ingest", {}); await force(conn, j.id, start)
    assert (await jobs.request(conn, j.id, action)).state == end


@pytest.mark.parametrize("action,start", [("pause", "done"), ("pause", "paused"), ("resume", "running"), ("retry", "done"), ("retry", "cancelled"), ("cancel", "done"), ("cancel", "cancelled")])
async def test_request_rejects_invalid(conn, action, start):
    j = await jobs.enqueue(conn, "ingest", {}); await force(conn, j.id, start)
    with pytest.raises(jobs.JobStateError) as e:
        await jobs.request(conn, j.id, action)
    assert (e.value.state, e.value.action) == (start, action) and (await jobs.get(conn, j.id)).state == start
    with pytest.raises(KeyError):
        await jobs.request(conn, "nope", action)


async def test_finish_and_release_never_clobber_a_request(conn):
    j = await jobs.enqueue(conn, "ingest", {}); await jobs.claim(conn, ["ingest"]); await jobs.request(conn, j.id, "pause")
    assert await jobs.finish(conn, j.id, "done") is False and await jobs.release(conn, j.id) is False
    assert await jobs.state(conn, j.id) == "paused"


async def test_progress_and_checkpoint(conn):
    j = await jobs.enqueue(conn, "ingest", {})
    await jobs.progress(conn, j.id, total=10, done=2, add={"ok": 2}, set={"embed_rate": 4.5})
    await jobs.progress(conn, j.id, done=1, add={"ok": 1, "skipped": 1})
    await jobs.checkpoint(conn, j.id, {"stage": "embed", "doc": "d"})
    g = await jobs.get(conn, j.id)
    assert (g.progress_total, g.progress_done) == (10, 3) and g.stats == {"ok": 3, "skipped": 1, "embed_rate": 4.5}
    assert g.checkpoint == {"stage": "embed", "doc": "d"} and g.state == "queued"


async def test_requeue_running_keeps_checkpoint_and_caps_attempts(conn):
    a = await jobs.enqueue(conn, "ingest", {}); b = await jobs.enqueue(conn, "ingest", {})
    await jobs.claim(conn, ["ingest"]); await jobs.claim(conn, ["ingest"])
    await jobs.checkpoint(conn, a.id, {"k": 1}); await force(conn, b.id, "running", attempts=5)
    assert await jobs.requeue_running(conn, ["ingest"]) == (1, 1)
    ga, gb = await jobs.get(conn, a.id), await jobs.get(conn, b.id)
    assert (ga.state, ga.attempts, ga.checkpoint) == ("queued", 1, {"k": 1})
    assert (gb.state, gb.error) == ("failed", "gave up after 5 attempts") and gb.finished_at


async def test_release_does_not_count_as_an_attempt(conn):
    j = await jobs.enqueue(conn, "ingest", {}); await jobs.claim(conn, ["ingest"])
    assert await jobs.release(conn, j.id) is True
    g = await jobs.get(conn, j.id)
    assert (g.state, g.attempts) == ("queued", 0)


async def test_resume_and_retry_reset_attempts(conn):
    j = await jobs.enqueue(conn, "ingest", {}); await force(conn, j.id, "paused", attempts=4)
    assert (await jobs.request(conn, j.id, "resume")).attempts == 0
    await force(conn, j.id, "failed", attempts=4, error="x", finished_at="t")
    g = await jobs.request(conn, j.id, "retry")
    assert (g.attempts, g.error, g.finished_at) == (0, None, None)


async def test_list_filters(conn):
    for i in range(60):
        await jobs.enqueue(conn, "ingest", {"collection_id": "c1" if i % 2 else "c2", "i": i})
    k = await jobs.enqueue(conn, "other", {"collection_id": "c1"}); await force(conn, k.id, "done")
    assert len(await jobs.list(conn, kind="ingest")) == 50 and len(await jobs.list(conn, kind="ingest", limit=None)) == 60
    mine = await jobs.list(conn, kind="ingest", payload_eq={"collection_id": "c1"}, limit=None)
    assert len(mine) == 30 and mine[0].payload["i"] == 59 and mine[-1].payload["i"] == 1
    assert [j.id for j in await jobs.list(conn, states=["done"])] == [k.id]


async def test_every_write_moves_updated_at_and_finish_sets_finished_at(conn):
    j = await jobs.enqueue(conn, "ingest", {}); await jobs.claim(conn, ["ingest"])

    async def stamp():
        await conn.execute("update jobs set updated_at='old' where id=?", (j.id,)); await conn.commit()
    for write in (lambda: jobs.checkpoint(conn, j.id, {}), lambda: jobs.progress(conn, j.id, done=1),
                  lambda: jobs.request(conn, j.id, "pause"), lambda: jobs.request(conn, j.id, "resume")):
        await stamp(); await write()
        assert (await jobs.get(conn, j.id)).updated_at != "old"
    await jobs.claim(conn, ["ingest"]); await stamp()
    assert await jobs.finish(conn, j.id, "failed", "boom") is True
    g = await jobs.get(conn, j.id)
    assert g.updated_at != "old" and g.finished_at and (g.state, g.error) == ("failed", "boom")
