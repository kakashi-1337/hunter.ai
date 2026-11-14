"""A persistent job queue: one table, atomic claim, and state changes that never clobber
a request the owner made while a worker held the job."""
from __future__ import annotations

import builtins
import json
from typing import Literal

from ..db import new_id, now
from ..types import Job

MAX_ATTEMPTS = 5

# action -> (allowed from, new state, extra SQL sets)
_ACTIONS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "pause": (("queued", "running"), "paused", ""),
    "resume": (("paused",), "queued", ", attempts=0"),
    "cancel": (("queued", "running", "paused", "failed"), "cancelled", ", finished_at=:now"),
    "retry": (("failed",), "queued", ", attempts=0, error=null, finished_at=null"),
}


class JobStateError(ValueError):
    def __init__(self, state: str, action: str):
        super().__init__(f"a {state} job cannot {action}")
        self.state, self.action = state, action


def _row(r) -> Job:
    return Job(id=r["id"], kind=r["kind"], state=r["state"], payload=json.loads(r["payload_json"]),
               checkpoint=json.loads(r["checkpoint_json"]), attempts=r["attempts"], error=r["error"],
               progress_total=r["progress_total"], progress_done=r["progress_done"],
               stats=json.loads(r["stats_json"]), created_at=r["created_at"],
               updated_at=r["updated_at"], started_at=r["started_at"], finished_at=r["finished_at"])


async def enqueue(conn, kind: str, payload: dict) -> Job:
    jid, t = new_id(), now()
    await conn.execute("insert into jobs(id,kind,state,payload_json,created_at,updated_at) "
                       "values(?,?,?,?,?,?)", (jid, kind, "queued", json.dumps(payload), t, t))
    await conn.commit()
    return await get(conn, jid)


async def get(conn, job_id: str) -> Job | None:
    r = await (await conn.execute("select * from jobs where id=?", (job_id,))).fetchone()
    return _row(r) if r else None


async def state(conn, job_id: str) -> str | None:
    r = await (await conn.execute("select state from jobs where id=?", (job_id,))).fetchone()
    return r["state"] if r else None


async def list(conn, kind: str | None = None, states: builtins.list[str] | None = None,  # noqa: A001
               payload_eq: dict | None = None, limit: int | None = 50) -> builtins.list[Job]:
    where, args = [], []
    if kind is not None:
        where.append("kind=?"); args.append(kind)
    if states:
        where.append(f"state in ({','.join('?' * len(states))})"); args += states
    for k, v in (payload_eq or {}).items():
        where.append("json_extract(payload_json, ?) = ?"); args += [f"$.{k}", v]
    sql = "select * from jobs" + (" where " + " and ".join(where) if where else "") + \
        " order by created_at desc, rowid desc"
    if limit is not None:
        sql += " limit ?"; args.append(limit)
    return [_row(r) for r in await (await conn.execute(sql, args)).fetchall()]


async def delete(conn, job_id: str) -> None:
    """Drop a job's row and its item list. Documents keep the id of the job that made them."""
    await conn.execute("delete from kb_job_items where job_id=?", (job_id,))
    await conn.execute("delete from jobs where id=?", (job_id,))
    await conn.commit()


async def claim(conn, kinds: builtins.list[str]) -> Job | None:
    """The oldest queued job of these kinds becomes running. One statement: two workers
    cannot both take it."""
    marks = ",".join("?" * len(kinds))
    t = now()
    r = await (await conn.execute(
        f"update jobs set state='running', attempts=attempts+1, updated_at=?, "
        f"started_at=coalesce(started_at, ?) where id = (select id from jobs where state='queued' "
        f"and kind in ({marks}) order by created_at, rowid limit 1) and state='queued' returning *",
        (t, t, *kinds))).fetchone()
    await conn.commit()
    return _row(r) if r else None


async def checkpoint(conn, job_id: str, data: dict) -> None:
    await conn.execute("update jobs set checkpoint_json=?, updated_at=? where id=?",
                       (json.dumps(data), now(), job_id))
    await conn.commit()


async def progress(conn, job_id: str, *, total: int | None = None, done: int = 0,
                   add: dict | None = None, set: dict | None = None,  # noqa: A002
                   commit: bool = True) -> None:
    r = await (await conn.execute("select stats_json from jobs where id=?", (job_id,))).fetchone()
    if not r:
        return
    stats = json.loads(r["stats_json"])
    for k, v in (add or {}).items():
        stats[k] = stats.get(k, 0) + v
    stats.update(set or {})
    await conn.execute(
        "update jobs set progress_total=coalesce(?, progress_total), progress_done=progress_done+?, "
        "stats_json=?, updated_at=? where id=?", (total, done, json.dumps(stats), now(), job_id))
    if commit:
        await conn.commit()


async def finish(conn, job_id: str, state: Literal["done", "failed"], error: str | None = None) -> bool:  # noqa: A002
    t = now()
    cur = await conn.execute("update jobs set state=?, error=?, finished_at=?, updated_at=? "
                             "where id=? and state='running'", (state, error, t, t, job_id))
    await conn.commit()
    return cur.rowcount == 1


async def release(conn, job_id: str) -> bool:
    """Back to queued without counting the attempt (a graceful stop)."""
    cur = await conn.execute("update jobs set state='queued', attempts=max(attempts-1, 0), updated_at=? "
                             "where id=? and state='running'", (now(), job_id))
    await conn.commit()
    return cur.rowcount == 1


async def request(conn, job_id: str, action: str) -> Job:
    allowed, new_state, extra = _ACTIONS[action]
    marks = ",".join(f"'{s}'" for s in allowed)
    t = now()
    cur = await conn.execute(
        f"update jobs set state=:new, updated_at=:now{extra} where id=:id and state in ({marks})",
        {"new": new_state, "now": t, "id": job_id})
    await conn.commit()
    job = await get(conn, job_id)
    if job is None:
        raise KeyError(job_id)
    if cur.rowcount != 1:
        raise JobStateError(job.state, action)
    return job


async def requeue_running(conn, kinds: builtins.list[str], max_attempts: int = MAX_ATTEMPTS) -> tuple[int, int]:
    """Crash recovery: running jobs go back to queued with their checkpoint; one that has
    already been tried max_attempts times fails instead."""
    marks = ",".join("?" * len(kinds))
    t = now()
    failed = await conn.execute(
        f"update jobs set state='failed', error='gave up after ' || attempts || ' attempts', "
        f"finished_at=?, updated_at=? where state='running' and kind in ({marks}) and attempts >= ?",
        (t, t, *kinds, max_attempts))
    requeued = await conn.execute(
        f"update jobs set state='queued', updated_at=? where state='running' and kind in ({marks})",
        (t, *kinds))
    await conn.commit()
    return requeued.rowcount, failed.rowcount
