"""'Allow for this chat' answers: one tool plus one normalized target per row."""
from __future__ import annotations

import builtins
from collections.abc import Sequence

from ..db import now
from ..permissions import Grant


async def list(conn, chat_id: str) -> builtins.list[Grant]:  # noqa: A001
    cur = await conn.execute(
        "select tool, target, warned from chat_grants where chat_id=? order by created_at, rowid",
        (chat_id,))
    return [Grant(r["tool"], r["target"], bool(r["warned"])) for r in await cur.fetchall()]


async def add(conn, chat_id: str, tool: str, target: str, warned: bool) -> None:
    await conn.execute(
        "insert into chat_grants(chat_id,tool,target,warned,created_at) values(?,?,?,?,?) "
        "on conflict(chat_id,tool,target) do update set warned=max(warned, excluded.warned)",
        (chat_id, tool, target, int(warned), now()))
    await conn.commit()


async def clear(conn, chat_id: str) -> int:
    cur = await conn.execute("delete from chat_grants where chat_id=?", (chat_id,))
    await conn.commit()
    return cur.rowcount


async def clear_tools(conn, chat_id: str, tools: Sequence[str]) -> int:
    tools = builtins.list(tools)
    if not tools:
        return 0
    marks = ",".join("?" * len(tools))
    cur = await conn.execute(
        f"delete from chat_grants where chat_id=? and tool in ({marks})", (chat_id, *tools))
    await conn.commit()
    return cur.rowcount
