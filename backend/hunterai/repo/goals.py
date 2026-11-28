"""The goal record of a chat: what it is trying to achieve, how success is judged, what is open, and what a
check said about its items."""
from __future__ import annotations

import json

from pydantic import BaseModel

from ..db import now

OWNER_OBJECTIVE = "The owner's checklist for this chat"      # of a record that was made for the owner's items
MAX_OWNER_ITEMS = 30        # one check reads 30 items (goal_check.MAX_ITEMS), and the owner's lead the list
MAX_ITEM = 300              # characters of an item the owner writes


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
    owner_items: list[str] = []        # what the owner wrote himself, open or done: only he takes one of them off
    updated_at: str


async def _owners(conn, chat_id: str) -> list[str]:
    """The items the owner wrote for the chat, in the order he wrote them."""
    rows = await (await conn.execute("select item from goal_owner_items where chat_id=? order by rowid", (chat_id,))).fetchall()
    return [r["item"] for r in rows]


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
                marks=marks, owner_items=await _owners(conn, chat_id), updated_at=r["updated_at"])


MAX_DONE = 60


async def put(conn, chat_id: str, objective: str, criteria: list[str], open_items: list[str],
              done_items: list[str] | None = None) -> Goal:
    # What the owner wrote is not an agent's to take off: whatever list is written, an item of his that is not done
    # is open, and none of his done ones is cut. His items lead the open list, in the order he wrote them.
    mine = await _owners(conn, chat_id)
    done = [i for i in dict.fromkeys(done_items or []) if i not in open_items]
    recent = done[-MAX_DONE:]
    done = [i for i in done if i in mine or i in recent]
    open_items = [*(i for i in mine if i not in done), *(i for i in open_items if i not in mine)]
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
    """The whole record, what the owner wrote onto it too: the owner clears it."""
    await conn.execute("delete from goals where chat_id=?", (chat_id,))
    await conn.execute("delete from goal_marks where chat_id=?", (chat_id,))
    await conn.execute("delete from goal_owner_items where chat_id=?", (chat_id,))
    await conn.commit()


async def restart(conn, chat_id: str, objective: str, criteria: list[str], open_items: list[str]) -> Goal:
    """A new goal from the agent: nothing of the old one stays, done or marked, but what the owner wrote. His items
    stay as they stand, open or done, each with its mark."""
    old, mine = await get(conn, chat_id), await _owners(conn, chat_id)
    for r in await (await conn.execute("select item from goal_marks where chat_id=?", (chat_id,))).fetchall():
        if r["item"] not in mine:
            await conn.execute("delete from goal_marks where chat_id=? and item=?", (chat_id, r["item"]))
    return await put(conn, chat_id, objective, criteria, open_items, [i for i in (old.done_items if old else []) if i in mine])


async def add_owner_items(conn, chat_id: str, items: list[str]) -> Goal:
    """The owner writes items onto the chat's record; where there is none, one is made for them. An item that is on
    the record already becomes his as it stands, open or done."""
    for item in items:
        await conn.execute("insert or ignore into goal_owner_items(chat_id,item,at) values(?,?,?)", (chat_id, item, now()))
    goal = await get(conn, chat_id)
    if goal is None:
        return await put(conn, chat_id, OWNER_OBJECTIVE, [], [])
    return await put(conn, chat_id, goal.objective, goal.criteria, goal.open_items, goal.done_items)


async def remove_owner_item(conn, chat_id: str, item: str) -> Goal | None:
    """The owner takes one of his items off: it leaves the record whole, open or done, and its mark with it. A record
    that was made for his items and holds nothing more goes with the last of them."""
    await conn.execute("delete from goal_owner_items where chat_id=? and item=?", (chat_id, item))
    goal = await get(conn, chat_id)
    if goal is None:
        await conn.commit()
        return None
    open_items, done = [i for i in goal.open_items if i != item], [i for i in goal.done_items if i != item]
    if goal.objective == OWNER_OBJECTIVE and not (open_items or done or goal.criteria):
        await delete(conn, chat_id)
        return None
    return await put(conn, chat_id, goal.objective, goal.criteria, open_items, done)
