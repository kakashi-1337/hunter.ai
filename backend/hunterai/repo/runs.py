from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import activity
from ..db import new_id, now
from ..types import RunDetail, RunSummary, Step


def _step(r) -> Step:
    return Step(id=r["id"], run_id=r["run_id"], seq=r["seq"], kind=r["kind"], name=r["name"],
                status=r["status"], started_at=r["started_at"], ended_at=r["ended_at"],
                input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                detail=json.loads(r["detail_json"]), parent_step_id=r["parent_step_id"])


def _ms(start: str, end: str | None) -> int:
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
    return max(0, int((b - a).total_seconds() * 1000))


def totals(run_row, steps: list[Step]) -> dict:
    return {
        "duration_ms": _ms(run_row["started_at"], run_row["ended_at"]),
        "turns": sum(1 for s in steps if s.kind == "model_call"),
        "calls": sum(1 for s in steps if s.kind == "tool_call"),
        "input_tokens": sum(s.input_tokens for s in steps),
        "output_tokens": sum(s.output_tokens for s in steps),
    }


async def _steps(conn, run_id: str) -> list[Step]:
    rows = await (await conn.execute("select * from steps where run_id=? order by seq",
                                     (run_id,))).fetchall()
    return [_step(r) for r in rows]


def _fields(r) -> dict:
    return dict(id=r["id"], chat_id=r["chat_id"], user_message_id=r["user_message_id"],
                assistant_message_id=r["assistant_message_id"], status=r["status"],
                error=r["error"], started_at=r["started_at"], ended_at=r["ended_at"])


async def create(conn, chat_id: str, user_message_id: str, assistant_message_id: str) -> RunDetail:
    rid = new_id()
    await conn.execute(
        "insert into runs(id,chat_id,user_message_id,assistant_message_id,status,started_at)"
        " values(?,?,?,?,?,?)", (rid, chat_id, user_message_id, assistant_message_id, "running",
                                 now()))
    await conn.commit()
    return await get(conn, rid)


async def finish(conn, run_id: str, status: str, error: str | None = None) -> None:
    await conn.execute("update runs set status=?, error=?, ended_at=? where id=?",
                       (status, error, now(), run_id))
    await conn.commit()


async def add_step(conn, run_id: str, kind: str, name: str, detail: dict,
                   parent_step_id: str | None = None) -> Step:
    sid = new_id()
    # The number is taken in the insert itself: subagents of a swarm add steps to one run at the same time.
    await conn.execute(
        "insert into steps(id,run_id,seq,kind,name,status,started_at,detail_json,parent_step_id)"
        " values(?,?,(select coalesce(max(seq), 0) + 1 from steps where run_id=?),?,?,?,?,?,?)",
        (sid, run_id, run_id, kind, name, "running", now(), json.dumps(detail or {}),
         parent_step_id))
    await conn.commit()
    return await get_step(conn, sid)


async def get_step(conn, step_id: str) -> Step | None:
    r = await (await conn.execute("select * from steps where id=?", (step_id,))).fetchone()
    return _step(r) if r else None


async def finish_step(conn, step_id: str, status: str, input_tokens: int = 0,
                      output_tokens: int = 0, detail: dict | None = None) -> Step:
    await conn.execute(
        "update steps set status=?, ended_at=?, input_tokens=?, output_tokens=? where id=?",
        (status, now(), input_tokens, output_tokens, step_id))
    if detail is not None:
        await conn.execute("update steps set detail_json=? where id=?",
                           (json.dumps(detail), step_id))
    await conn.commit()
    step = await get_step(conn, step_id)
    activity.record(step)
    return step


async def get(conn, run_id: str) -> RunDetail | None:
    r = await (await conn.execute("select * from runs where id=?", (run_id,))).fetchone()
    if not r:
        return None
    steps = await _steps(conn, run_id)
    return RunDetail(**_fields(r), totals=totals(r, steps), steps=steps)


async def list_for_chat(conn, chat_id: str) -> list[RunSummary]:
    rows = await (await conn.execute(
        "select * from runs where chat_id=? order by started_at, rowid", (chat_id,))).fetchall()
    return [RunSummary(**_fields(r), totals=totals(r, await _steps(conn, r["id"])))
            for r in rows]


async def has_running(conn, chat_id: str) -> bool:
    r = await (await conn.execute(
        "select 1 from runs where chat_id=? and status='running' limit 1", (chat_id,))).fetchone()
    return r is not None


async def running_ids(conn, chat_id: str) -> list[str]:
    rows = await (await conn.execute(
        "select id from runs where chat_id=? and status='running'", (chat_id,))).fetchall()
    return [r["id"] for r in rows]


async def mark_interrupted(conn) -> int:
    t = now()
    cur = await conn.execute(
        "update runs set status='interrupted', ended_at=?,"
        " error='The app stopped while this run was in progress.' where status='running'", (t,))
    await conn.execute(
        "update steps set status='interrupted', ended_at=? where status='running'", (t,))
    await conn.commit()
    return cur.rowcount


async def patch_step(conn, step_id: str, *, status: str | None = None,
                     detail: dict | None = None, close: bool = False) -> Step:
    """Merge `detail` into the stored detail; set the status and close the step when asked."""
    if detail:
        cur = await get_step(conn, step_id)
        merged = {**(cur.detail if cur else {}), **detail}
        await conn.execute("update steps set detail_json=? where id=?",
                           (json.dumps(merged), step_id))
    if status is not None:
        await conn.execute("update steps set status=? where id=?", (status, step_id))
    if close:
        await conn.execute("update steps set ended_at=? where id=?", (now(), step_id))
    await conn.commit()
    step = await get_step(conn, step_id)
    if close:
        activity.record(step)
    return step
