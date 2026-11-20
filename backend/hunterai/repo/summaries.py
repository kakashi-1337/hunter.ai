"""What stands in for the older part of a chat that no longer fits its model's context
window. A summary covers the chat's path up to one message; a chat with branches can hold
one per branch."""
from __future__ import annotations

from ..db import now


async def latest(conn, chat_id: str, path_ids: list[str]) -> tuple[str, str] | None:
    """(the message it reaches to, the summary) of the summary that covers the most of this
    path, or None. A summary of another branch is not on the path and is not used."""
    place = {mid: n for n, mid in enumerate(path_ids)}
    rows = await (await conn.execute("select upto_message_id, summary from chat_summaries where chat_id=?",
                                     (chat_id,))).fetchall()
    on_path = [r for r in rows if r["upto_message_id"] in place]
    if not on_path:
        return None
    best = max(on_path, key=lambda r: place[r["upto_message_id"]])
    return best["upto_message_id"], best["summary"]


async def put(conn, chat_id: str, upto_message_id: str, summary: str) -> None:
    await conn.execute(
        "insert into chat_summaries(chat_id,upto_message_id,summary,created_at) values(?,?,?,?)"
        " on conflict(chat_id,upto_message_id) do update set summary=excluded.summary, created_at=excluded.created_at",
        (chat_id, upto_message_id, summary, now()))
    await conn.commit()
