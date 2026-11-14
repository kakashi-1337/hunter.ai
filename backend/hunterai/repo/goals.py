"""The goal record of a chat: what it is trying to achieve, how success is judged, what is open."""
from __future__ import annotations

import json

from pydantic import BaseModel

from ..db import now


class Goal(BaseModel):
    chat_id: str
    objective: str
    criteria: list[str] = []
    open_items: list[str] = []
    done_items: list[str] = []         # items that left the open list, oldest first
    updated_at: str


async def get(conn, chat_id: str) -> Goal | None:
    r = await (await conn.execute("select * from goals where chat_id=?", (chat_id,))).fetchone()
    if not r:
        return None
    return Goal(chat_id=r["chat_id"], objective=r["objective"], criteria=json.loads(r["criteria_json"]),
                open_items=json.loads(r["open_items_json"]),
                done_items=json.loads(r["done_items_json"] or "[]") if "done_items_json" in r.keys() else [],
                updated_at=r["updated_at"])


MAX_DONE = 60


async def put(conn, chat_id: str, objective: str, criteria: list[str], open_items: list[str],
              done_items: list[str] | None = None) -> Goal:
    done = [i for i in dict.fromkeys(done_items or []) if i not in open_items][-MAX_DONE:]
    await conn.execute(
        "insert into goals(chat_id,objective,criteria_json,open_items_json,done_items_json,updated_at) values(?,?,?,?,?,?) "
        "on conflict(chat_id) do update set objective=excluded.objective, "
        "criteria_json=excluded.criteria_json, open_items_json=excluded.open_items_json, "
        "done_items_json=excluded.done_items_json, updated_at=excluded.updated_at",
        (chat_id, objective, json.dumps(criteria), json.dumps(open_items), json.dumps(done), now()))
    await conn.commit()
    return await get(conn, chat_id)


async def delete(conn, chat_id: str) -> None:
    await conn.execute("delete from goals where chat_id=?", (chat_id,))
    await conn.commit()
