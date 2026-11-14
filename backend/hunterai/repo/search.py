from __future__ import annotations

from ..types import SearchHit


def to_match(q: str) -> str:
    """Turn free text into an FTS5 query of quoted terms (implicit AND).

    Quoting every term makes FTS operators and punctuation literal, so no
    user input can produce a syntax error.
    """
    terms = [t.replace('"', '""') for t in q.replace("\x00", " ").split()]
    return " ".join(f'"{t}"' for t in terms if t.strip('"'))


async def query(conn, q: str, limit: int = 50) -> list[SearchHit]:
    match = to_match(q or "")
    if not match:
        return []
    rows = await (await conn.execute(
        "select f.message_id, f.chat_id, c.title,"
        " snippet(messages_fts, 2, '', '', '…', 16) as snip"
        " from messages_fts f join chats c on c.id=f.chat_id"
        " where messages_fts match ? order by rank limit ?", (match, limit))).fetchall()
    return [SearchHit(chat_id=r["chat_id"], chat_title=r["title"], message_id=r["message_id"],
                      snippet=r["snip"]) for r in rows]
