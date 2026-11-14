from __future__ import annotations

import builtins
import json
from pathlib import Path

from .. import config
from ..db import new_id, now
from ..types import Chat

_FIELDS = {"title", "mode", "provider", "model", "options", "target_id", "workdir",
           "permissions", "persona_id"}
_JSON = {"options": "options_json", "permissions": "permissions_json"}


def _row(r) -> Chat:
    return Chat(id=r["id"], target_id=r["target_id"], title=r["title"], mode=r["mode"],
                provider=r["provider"], model=r["model"], options=json.loads(r["options_json"]),
                active_leaf_id=r["active_leaf_id"], created_at=r["created_at"],
                updated_at=r["updated_at"], workdir=r["workdir"],
                permissions=json.loads(r["permissions_json"] or "{}"), persona_id=r["persona_id"])


def resolve_workdir(chat: Chat) -> Path:
    """The chat's working folder: its own setting, else the default under the data folder."""
    return Path(chat.workdir) if chat.workdir else config.work_dir(chat.id)


async def create(conn, target_id: str | None, mode: str, provider: str, model: str,
                 options: dict, persona_id: str | None = None) -> Chat:
    cid, t = new_id(), now()
    await conn.execute(
        "insert into chats(id,target_id,title,mode,provider,model,options_json,created_at,"
        "updated_at,persona_id) values(?,?,?,?,?,?,?,?,?,?)",
        (cid, target_id, "New chat", mode, provider, model, json.dumps(options or {}), t, t, persona_id))
    await conn.commit()
    return await get(conn, cid)


async def get(conn, cid: str) -> Chat | None:
    r = await (await conn.execute("select * from chats where id=?", (cid,))).fetchone()
    return _row(r) if r else None


async def list(conn, target_id: str | None = None) -> builtins.list[Chat]:  # noqa: A001
    if target_id is None:
        cur = await conn.execute("select * from chats order by updated_at desc, rowid desc")
    else:
        cur = await conn.execute(
            "select * from chats where target_id=? order by updated_at desc, rowid desc",
            (target_id,))
    return [_row(r) for r in await cur.fetchall()]


async def update(conn, cid: str, **fields) -> Chat:
    bad = set(fields) - _FIELDS
    if bad:
        raise ValueError(f"cannot update {sorted(bad)}")
    for k, v in fields.items():
        col, val = (_JSON[k], json.dumps(v or {})) if k in _JSON else (k, v)
        await conn.execute(f"update chats set {col}=? where id=?", (val, cid))
    await conn.execute("update chats set updated_at=? where id=?", (now(), cid))
    await conn.commit()
    return await get(conn, cid)


async def delete(conn, cid: str) -> None:
    await conn.execute("delete from messages_fts where chat_id=?", (cid,))
    await conn.execute("delete from chats where id=?", (cid,))
    await conn.commit()
