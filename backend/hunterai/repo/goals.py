"""The goal record of a chat: what it is trying to achieve, how success is judged, what is open, and what a
check said about its items."""
from __future__ import annotations

import json

from pydantic import BaseModel

from ..db import now


class Mark(BaseModel):
    """What the check said about one item (see goal_check.py). `shown` is the only verdict that makes it done."""
    verdict: str            # shown | not_shown | contradicted | unchecked
    evidence: str = ""      # the words of a tool result that decide it; else why there is no verdict
    tool: str = ""          # the tool whose result holds those words
    model: str = ""         # the decision model that read the results
    at: str = ""


class Goal(BaseModel):
    chat_id: str
    objective: str
    criteria: list[str] = []
    open_items: list[str] = []
    done_items: list[str] = []         # items a tool result showed as done, oldest first
    marks: dict[str, Mark] = {}        # by item: of the done ones, and of the open ones that were said to be done
    updated_at: str


async def get(conn, chat_id: str) -> Goal | None:
    r = await (await conn.execute("select * from goals where chat_id=?", (chat_id,))).fetchone()
    if not r:
        return None
    rows = await (await conn.execute("select * from goal_marks where chat_id=?", (chat_id,))).fetchall()
    marks = {m["item"]: Mark(verdict=m["verdict"], evidence=m["evidence"], tool=m["tool"], model=m["model"], at=m["at"])
             for m in rows}
    return Goal(chat_id=r["chat_id"], objective=r["objective"], criteria=json.loads(r["criteria_json"]),
                open_items=json.loads(r["open_items_json"]),
                done_items=json.loads(r["done_items_json"] or "[]") if "done_items_json" in r.keys() else [],
                marks=marks, updated_at=r["updated_at"])


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
    # A mark belongs to an item of the record: it goes with an item that left, and an item that is open again is
    # not shown as done any more.
    for r in await (await conn.execute("select item, verdict from goal_marks where chat_id=?", (chat_id,))).fetchall():
        if r["item"] not in done and (r["item"] not in open_items or r["verdict"] == "shown"):
            await conn.execute("delete from goal_marks where chat_id=? and item=?", (chat_id, r["item"]))
    await conn.commit()
    return await get(conn, chat_id)


async def mark(conn, chat_id: str, item: str, mark: Mark) -> None:
    await conn.execute(
        "insert into goal_marks(chat_id,item,verdict,evidence,tool,model,at) values(?,?,?,?,?,?,?) "
        "on conflict(chat_id,item) do update set verdict=excluded.verdict, evidence=excluded.evidence, "
        "tool=excluded.tool, model=excluded.model, at=excluded.at",
        (chat_id, item, mark.verdict, mark.evidence, mark.tool, mark.model, mark.at or now()))
    await conn.commit()


async def delete(conn, chat_id: str) -> None:
    await conn.execute("delete from goals where chat_id=?", (chat_id,))
    await conn.execute("delete from goal_marks where chat_id=?", (chat_id,))
    await conn.commit()
