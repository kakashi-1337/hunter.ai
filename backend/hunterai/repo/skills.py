"""Skills: instructions for a kind of work, kept under a name that is also a slash command. The text is in
the database, which no tool writes; a skill's other files, when it has any, are in its folder."""
from __future__ import annotations

import builtins
import sqlite3
from pathlib import Path

from pydantic import BaseModel

from ..db import new_id, now
from . import settings


class Skill(BaseModel):
    id: str
    name: str
    description: str = ""
    body: str
    mode: str | None = None          # the mode its slash command switches the chat to first
    source: str = ""                 # where it came from
    folder: str | None = None        # where its other files are
    files: list[str] = []            # their names, as the model is told them
    enabled: bool = True
    auto: bool = True                # the model may load it by itself
    builtin: bool = False
    created_at: str
    updated_at: str


def files_in(folder: str | None) -> list[str]:
    root = Path(folder) if folder else None
    if root is None or not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def _row(r) -> Skill:
    return Skill(id=r["id"], name=r["name"], description=r["description"], body=r["body"], mode=r["mode"],
                 source=r["source"], folder=r["folder"], files=files_in(r["folder"]), enabled=bool(r["enabled"]),
                 auto=bool(r["auto"]), builtin=bool(r["builtin"]), created_at=r["created_at"], updated_at=r["updated_at"])


SEEDED = "skills_seeded"            # true once an install was given built-in skills: all an install kept at first
GIVEN = "skills_given"              # the names of the built-in skills this install was ever given
FIRST = ("plan", "audit", "debug")  # what an install was given that was seeded before the names were kept


async def ensure_builtins(conn) -> None:
    """Each built-in skill, once per install. Deleting or rewriting one later is the owner's choice and stays.
    A built-in skill that is new in this version arrives once; a skill of the owner's with its name is kept."""
    given = await settings.get(conn, GIVEN)
    if not isinstance(given, builtins.list):            # `list` is a function of this module
        given = [*FIRST] if await settings.get(conn, SEEDED) else []
    from ..skills import BUILTINS
    new = [b for b in BUILTINS if b[0] not in given]
    if not new:
        return
    t = now()
    for name, description, mode, body in new:
        await conn.execute("insert or ignore into skills(id,name,description,body,mode,source,builtin,created_at,updated_at) "
                           "values(?,?,?,?,?,'built in',1,?,?)", (new_id(), name, description, body, mode, t, t))
    await conn.commit()
    await settings.put(conn, GIVEN, [*given, *(b[0] for b in new)])
    await settings.put(conn, SEEDED, True)


async def list(conn) -> list[Skill]:  # noqa: A001
    await ensure_builtins(conn)
    rows = await (await conn.execute("select * from skills order by name collate nocase")).fetchall()
    return [_row(r) for r in rows]


async def get(conn, sid: str) -> Skill | None:
    r = await (await conn.execute("select * from skills where id=?", (sid,))).fetchone()
    return _row(r) if r else None


async def by_name(conn, name: str) -> Skill | None:
    await ensure_builtins(conn)
    r = await (await conn.execute("select * from skills where name=? collate nocase", (name,))).fetchone()
    return _row(r) if r else None


async def create(conn, *, name: str, description: str = "", body: str, mode: str | None = None, source: str = "",
                 folder: str | None = None, enabled: bool = True, auto: bool = True) -> Skill:
    await ensure_builtins(conn)
    sid, t = new_id(), now()
    try:
        await conn.execute(
            "insert into skills(id,name,description,body,mode,source,folder,enabled,auto,created_at,updated_at) "
            "values(?,?,?,?,?,?,?,?,?,?,?)", (sid, name, description, body, mode, source, folder, int(enabled), int(auto), t, t))
    except sqlite3.IntegrityError:
        await conn.rollback()
        raise ValueError("name taken") from None
    await conn.commit()
    return await get(conn, sid)


_COLUMNS = ("name", "description", "body", "mode", "folder", "enabled", "auto")


async def update(conn, sid: str, **fields) -> Skill:
    try:
        for k, v in fields.items():
            if k not in _COLUMNS:
                raise KeyError(k)
            await conn.execute(f"update skills set {k}=? where id=?", (int(v) if isinstance(v, bool) else v, sid))
        await conn.execute("update skills set updated_at=? where id=?", (now(), sid))
    except sqlite3.IntegrityError:
        await conn.rollback()
        raise ValueError("name taken") from None
    await conn.commit()
    return await get(conn, sid)


async def delete(conn, sid: str) -> None:
    await conn.execute("delete from skills where id=?", (sid,))
    await conn.commit()
