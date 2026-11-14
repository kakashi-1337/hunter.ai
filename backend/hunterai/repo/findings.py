"""Findings recorded against a target."""
from __future__ import annotations

import builtins
import json

from pydantic import BaseModel

from ..db import new_id, now

SEVERITIES = ["info", "low", "medium", "high", "critical"]
STATUSES = ["open", "confirmed", "false_positive", "duplicate", "reported", "fixed"]


class Finding(BaseModel):
    id: str
    target_id: str
    title: str
    severity: str
    status: str
    evidence: str
    chat_id: str | None = None
    step_id: str | None = None
    triage: dict = {}
    created_at: str
    notified_at: str | None = None     # when it was announced to the owner's Discord channel, if it was


def _row(r) -> Finding:
    return Finding(id=r["id"], target_id=r["target_id"], title=r["title"], severity=r["severity"],
                   status=r["status"], evidence=r["evidence"], chat_id=r["chat_id"], step_id=r["step_id"],
                   triage=json.loads(r["triage_json"] or "{}"), created_at=r["created_at"],
                   notified_at=r["notified_at"] if "notified_at" in r.keys() else None)


async def add(conn, target_id: str, title: str, severity: str, evidence: str, *, chat_id: str | None = None,
              step_id: str | None = None, triage: dict | None = None) -> Finding:
    fid = new_id()
    await conn.execute(
        "insert into findings(id,target_id,title,severity,status,evidence,chat_id,step_id,triage_json,"
        "created_at) values(?,?,?,?,?,?,?,?,?,?)",
        (fid, target_id, title, severity, "open", evidence, chat_id, step_id, json.dumps(triage or {}), now()))
    await conn.commit()
    return await get(conn, fid)


async def get(conn, fid: str) -> Finding | None:
    r = await (await conn.execute("select * from findings where id=?", (fid,))).fetchone()
    return _row(r) if r else None


async def list_for_target(conn, target_id: str, status: str | None = None) -> builtins.list[Finding]:
    sql, args = "select * from findings where target_id=?", [target_id]
    if status:
        sql, args = sql + " and status=?", [target_id, status]
    rows = await (await conn.execute(sql + " order by created_at, rowid", args)).fetchall()
    return [_row(r) for r in rows]


async def list_all(conn) -> builtins.list[dict]:
    """Every finding, the newest first, with the name of its target and the title of its chat (None when that chat is gone)."""
    rows = await (await conn.execute(
        "select f.*, t.name as target_name, c.title as chat_title from findings f join targets t on t.id=f.target_id "
        "left join chats c on c.id=f.chat_id order by f.created_at desc, f.rowid desc")).fetchall()
    return [{**_row(r).model_dump(), "target_name": r["target_name"], "chat_title": r["chat_title"]} for r in rows]


async def update(conn, fid: str, **fields) -> Finding:
    for key in ("title", "severity", "status", "evidence"):
        if fields.get(key) is not None:
            await conn.execute(f"update findings set {key}=? where id=?", (fields[key], fid))
    await conn.commit()
    return await get(conn, fid)


async def delete(conn, fid: str) -> None:
    await conn.execute("delete from findings where id=?", (fid,))
    await conn.commit()
