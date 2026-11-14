from __future__ import annotations

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
           ("goals", "done_items_json", "text not null default '[]'")]


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(config.db_path())
    conn.row_factory = aiosqlite.Row
    await conn.execute("pragma journal_mode=wal")
    await conn.execute("pragma foreign_keys=on")
    await conn.execute("pragma busy_timeout=5000")
    return conn


async def migrate(conn: aiosqlite.Connection) -> None:
    await conn.executescript(SCHEMA.read_text())
    for table, column, ddl in COLUMNS:
        have = {r["name"] for r in await (await conn.execute(f"pragma table_info({table})")).fetchall()}
        if column not in have:
            await conn.execute(f"alter table {table} add column {column} {ddl}")
    await conn.execute(f"pragma user_version={VERSION}")
    await conn.commit()
