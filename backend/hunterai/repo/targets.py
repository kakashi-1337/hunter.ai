from __future__ import annotations

import json

from ..db import new_id, now
from ..types import Target

_FIELDS = {"name", "notes", "scope"}


def _row(r) -> Target:
    return Target(id=r["id"], name=r["name"], notes=r["notes"],
                  scope=json.loads(r["scope_json"]), created_at=r["created_at"])


def _scope(scope: dict | None) -> dict:
    scope = scope or {}
    return {"in": [str(x) for x in scope.get("in", [])],
            "out": [str(x) for x in scope.get("out", [])]}


async def create(conn, name: str, notes: str = "", scope: dict | None = None) -> Target:
    tid = new_id()
    await conn.execute(
        "insert into targets(id,name,notes,scope_json,created_at) values(?,?,?,?,?)",
        (tid, name, notes, json.dumps(_scope(scope)), now()))
    await conn.commit()
    return await get(conn, tid)


async def get(conn, tid: str) -> Target | None:
    r = await (await conn.execute("select * from targets where id=?", (tid,))).fetchone()
    return _row(r) if r else None


async def list(conn) -> list[Target]:  # noqa: A001 - repo API name
    rows = await (await conn.execute("select * from targets order by created_at")).fetchall()
    return [_row(r) for r in rows]


async def update(conn, tid: str, **fields) -> Target:
    bad = set(fields) - _FIELDS
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    for k, v in fields.items():
        col, val = ("scope_json", json.dumps(_scope(v))) if k == "scope" else (k, v)
        await conn.execute(f"update targets set {col}=? where id=?", (val, tid))
    await conn.commit()
    return await get(conn, tid)


async def delete(conn, tid: str) -> None:
    await conn.execute("delete from targets where id=?", (tid,))
    await conn.commit()
