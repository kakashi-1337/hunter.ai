from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from . import config

SCHEMA = Path(__file__).with_name("schema.sql")
VERSION = 5
# Columns added after a table first shipped: (table, column, ddl). schema.sql carries them
# for fresh databases; migrate() adds them to older ones.
COLUMNS = [("chats", "workdir", "text"),
           ("chats", "permissions_json", "text not null default '{}'"),
           ("steps", "parent_step_id", "text"),
           ("findings", "triage_json", "text not null default '{}'"),
           ("chats", "persona_id", "text"),
           ("findings", "notified_at", "text"),
           ("goals", "done_items_json", "text not null default '[]'"),
           ("chat_kb", "emptied", "integer not null default 0"),
           ("chat_kb", "retrieve_chosen", "integer not null default 0")]


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def connect() -> aiosqlite.Connection:
    """A connection with HunterAI's settings, for the caller to close.

    Safe to cancel. A connection has a worker thread of its own, and one that nobody closes keeps the
    whole process from exiting. So the opening runs to its end even when the caller is cancelled
    meanwhile (Stop pressed as a command starts), and a connection that then has no one to hand it
    to is closed here."""
    opening = asyncio.ensure_future(_open())
    try:
        return await asyncio.shield(opening)
    except asyncio.CancelledError:
        opening.add_done_callback(_close_abandoned)
        raise


async def _open() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(config.db_path())
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("pragma journal_mode=wal")
        await conn.execute("pragma foreign_keys=on")
        await conn.execute("pragma busy_timeout=5000")
    except BaseException:
        await conn.close()
        raise
    return conn


_closing: set[asyncio.Future] = set()      # held until done: the loop keeps only weak references to tasks


def _close_abandoned(opening: asyncio.Future) -> None:
    if opening.cancelled() or opening.exception() is not None:
        return                                  # nothing was opened, or _open closed it itself
    closing = asyncio.ensure_future(opening.result().close())
    _closing.add(closing)
    closing.add_done_callback(_closing.discard)


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA.read_text())
    for table, column, ddl in COLUMNS:
        have = {r["name"] for r in await (await conn.execute(f"pragma table_info({table})")).fetchall()}
        if column not in have:
            await conn.execute(f"alter table {table} add column {column} {ddl}")
    await conn.execute(f"pragma user_version={VERSION}")
    await conn.commit()
