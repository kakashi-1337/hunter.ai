from __future__ import annotations

from ..db import new_id, now
from ..types import Block, BlockList, Message

_UNSET = object()


def _row(r) -> Message:
    return Message(id=r["id"], chat_id=r["chat_id"], parent_id=r["parent_id"], role=r["role"],
                   blocks=BlockList.validate_json(r["blocks_json"]), provider=r["provider"],
                   model=r["model"], fallback_from=r["fallback_from"], status=r["status"],
                   created_at=r["created_at"])


def _dump(blocks: list[Block]) -> str:
    return BlockList.dump_json(blocks).decode()


def _search_text(blocks: list[Block]) -> str:
    return "\n".join(b.text for b in blocks if b.type == "text" and b.text)


async def _index(conn, mid: str, chat_id: str, blocks: list[Block]) -> None:
    await conn.execute("delete from messages_fts where message_id=?", (mid,))
    text = _search_text(blocks)
    if text:
        await conn.execute("insert into messages_fts(message_id,chat_id,text) values(?,?,?)",
                           (mid, chat_id, text))


async def add(conn, chat_id: str, parent_id: str | None, role: str, blocks: list[Block], *,
              provider: str | None = None, model: str | None = None,
              status: str = "complete") -> Message:
    mid, t = new_id(), now()
    await conn.execute(
        "insert into messages(id,chat_id,parent_id,role,blocks_json,provider,model,status,"
        "created_at) values(?,?,?,?,?,?,?,?,?)",
        (mid, chat_id, parent_id, role, _dump(blocks), provider, model, status, t))
    await conn.execute("update chats set active_leaf_id=?, updated_at=? where id=?",
                       (mid, t, chat_id))
    await _index(conn, mid, chat_id, blocks)
    await conn.commit()
    return await get(conn, mid)


async def get(conn, mid: str) -> Message | None:
    r = await (await conn.execute("select * from messages where id=?", (mid,))).fetchone()
    return _row(r) if r else None


async def update(conn, mid: str, *, blocks=None, status=None, provider=None, model=None,
                 fallback_from=_UNSET) -> Message:
    if blocks is not None:
        await conn.execute("update messages set blocks_json=? where id=?", (_dump(blocks), mid))
        r = await (await conn.execute("select chat_id from messages where id=?",
                                      (mid,))).fetchone()
        if r:
            await _index(conn, mid, r["chat_id"], blocks)
    for col, val in (("status", status), ("provider", provider), ("model", model)):
        if val is not None:
            await conn.execute(f"update messages set {col}=? where id=?", (val, mid))
    if fallback_from is not _UNSET:
        await conn.execute("update messages set fallback_from=? where id=?",
                           (fallback_from, mid))
    await conn.commit()
    return await get(conn, mid)


async def path(conn, chat_id: str, leaf_id: str | None = None) -> list[Message]:
    """Messages from the root down to leaf_id (default: the chat's active leaf)."""
    if leaf_id is None:
        r = await (await conn.execute("select active_leaf_id from chats where id=?",
                                      (chat_id,))).fetchone()
        leaf_id = r["active_leaf_id"] if r else None
    if not leaf_id:
        return []
    rows = await (await conn.execute(
        "with recursive up(id, depth) as ("
        "  select ?, 0 union all"
        "  select m.parent_id, up.depth+1 from messages m join up on m.id=up.id"
        "  where m.parent_id is not null)"
        " select m.* from messages m join up on m.id=up.id where m.chat_id=?"
        " order by up.depth desc", (leaf_id, chat_id))).fetchall()
    return [_row(r) for r in rows]


async def siblings(conn, mid: str) -> list[str]:
    r = await (await conn.execute("select chat_id, parent_id from messages where id=?",
                                  (mid,))).fetchone()
    if not r:
        return []
    rows = await (await conn.execute(
        "select id from messages where chat_id=? and parent_id is ? order by created_at, rowid",
        (r["chat_id"], r["parent_id"]))).fetchall()
    return [x["id"] for x in rows]


async def set_active(conn, chat_id: str, message_id: str) -> None:
    """Make the branch through message_id active, descending by newest child."""
    leaf = message_id
    while True:
        r = await (await conn.execute(
            "select id from messages where parent_id=? and chat_id=?"
            " order by created_at desc, rowid desc limit 1", (leaf, chat_id))).fetchone()
        if not r:
            break
        leaf = r["id"]
    await conn.execute("update chats set active_leaf_id=? where id=?", (leaf, chat_id))
    await conn.commit()


async def subtree_ids(conn, mid: str) -> list[str]:
    rows = await (await conn.execute(
        "with recursive down(id) as (select ? union all"
        " select m.id from messages m join down on m.parent_id=down.id)"
        " select id from down", (mid,))).fetchall()
    return [r["id"] for r in rows]


async def delete(conn, mid: str) -> None:
    m = await get(conn, mid)
    if not m:
        return
    ids = await subtree_ids(conn, mid)
    r = await (await conn.execute("select active_leaf_id from chats where id=?",
                                  (m.chat_id,))).fetchone()
    if r and r["active_leaf_id"] in ids:
        await conn.execute("update chats set active_leaf_id=? where id=?",
                           (m.parent_id, m.chat_id))
    for i in ids:
        await conn.execute("delete from messages_fts where message_id=?", (i,))
    await conn.execute("delete from messages where id=?", (mid,))
    await conn.commit()
