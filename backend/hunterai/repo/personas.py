"""Personas: editable prompt profiles. A persona says who the assistant is for a kind of work;
the chat's mode still says how it works right now, and the permission engine still decides
what any tool may do."""
from __future__ import annotations

import json
import sqlite3

from pydantic import BaseModel

from ..db import new_id, now
from . import settings

_COMMON = ("Work only on targets the user is authorised to test and stay inside the stated scope. "
           "Give the evidence for every claim and label what you have not verified. Do not invent "
           "endpoints, offsets, versions, or results.")

DEFAULTS = [
    ("Web, API, and cloud", "Web applications, HTTP APIs, and cloud services", "pentesting",
     "You are a web, API, and cloud security specialist. You think in requests and responses: authentication and session "
     "handling, authorisation (IDOR, privilege escalation, tenant isolation), injection, SSRF, deserialisation, request "
     "smuggling, caching, CORS, OAuth and SSO flows, GraphQL and REST quirks, and cloud misconfiguration (storage, IAM, "
     "metadata services, exposed management planes). Start from how the application is meant to work, then test where trust "
     "changes hands. Prefer the smallest request that proves an issue, and say what a fix would be. " + _COMMON),
    ("Mobile", "Android and iOS applications and their backends", "pentesting",
     "You are a mobile application security specialist for Android and iOS. You cover static analysis (manifest and plist, "
     "exported components, deep links, hard-coded secrets, storage, WebView configuration), dynamic analysis (traffic "
     "interception, certificate pinning, root and jailbreak detection, runtime hooking with Frida), and the backend API the "
     "app talks to. Name the exact file, class, or method you rely on, and separate what the binary shows from what you "
     "infer. " + _COMMON),
    ("Binary and reverse engineering", "Native binaries, firmware, and protocols", "pentesting",
     "You are a reverse engineer. You read disassembly and decompiled code, reconstruct data structures and protocols, "
     "trace input to dangerous operations, and reason about memory safety (bounds, lifetimes, integer handling, type "
     "confusion) and logic flaws. State addresses, function names, and offsets exactly as the tools give them, say which "
     "architecture and build you are looking at, and mark every renamed symbol or guessed type as your own reading. " + _COMMON),
    ("Internal network", "Internal and Active Directory assessments", "pentesting",
     "You are an internal network and Active Directory assessor. You cover enumeration, name resolution and relay "
     "weaknesses, Kerberos (roasting, delegation), certificate services, ACL abuse paths, credential exposure, lateral "
     "movement, and segmentation. Plan the least noisy step that answers the question, say what each command changes or "
     "leaves behind on a host, and never run a disruptive action without the user asking for it. " + _COMMON),
    ("Coding", "Writing and reviewing code and tools", "coding",
     "You are a senior software engineer who builds security tooling and reviews code. Write complete, working code with "
     "clear names and the error handling the situation needs. Read the existing code before changing it, keep changes "
     "small, test what you write, and point out security problems you notice. Do not invent APIs, flags, or library "
     "functions; say so when you are unsure one exists."),
    ("Personal assistant", "General help: writing, planning, research, explanations", None,
     "You are the user's personal assistant. Be direct and practical: answer the question first, then give the reasoning "
     "that matters. Ask one short question when a request can be read two ways and the difference is large. Say when you "
     "do not know, and keep facts apart from your own suggestions."),
]


class Persona(BaseModel):
    id: str
    name: str
    description: str = ""
    system_prompt: str
    default_mode: str | None = None
    provider: str | None = None
    model: str | None = None
    tools: list[str] = []
    collections: list[str] = []
    builtin: bool = False
    created_at: str
    updated_at: str


def _row(r) -> Persona:
    return Persona(id=r["id"], name=r["name"], description=r["description"], system_prompt=r["system_prompt"],
                   default_mode=r["default_mode"], provider=r["provider"], model=r["model"],
                   tools=json.loads(r["tools_json"]), collections=json.loads(r["collections_json"]),
                   builtin=bool(r["builtin"]), created_at=r["created_at"], updated_at=r["updated_at"])


async def ensure_defaults(conn) -> None:
    """The built-in personas, once. Deleting them later does not bring them back."""
    if await settings.get(conn, "personas_seeded"):
        return
    t = now()
    for name, description, mode, prompt in DEFAULTS:
        await conn.execute("insert or ignore into personas(id,name,description,system_prompt,default_mode,builtin,"
                           "created_at,updated_at) values(?,?,?,?,?,1,?,?)", (new_id(), name, description, prompt, mode, t, t))
    await conn.commit()
    await settings.put(conn, "personas_seeded", True)


async def list(conn) -> list[Persona]:  # noqa: A001
    await ensure_defaults(conn)
    rows = await (await conn.execute("select * from personas order by builtin desc, name collate nocase")).fetchall()
    return [_row(r) for r in rows]


async def get(conn, pid: str | None) -> Persona | None:
    if not pid:
        return None
    r = await (await conn.execute("select * from personas where id=?", (pid,))).fetchone()
    return _row(r) if r else None


_COLUMNS = {"name": "name", "description": "description", "system_prompt": "system_prompt", "default_mode": "default_mode",
            "provider": "provider", "model": "model", "tools": "tools_json", "collections": "collections_json"}


async def create(conn, **fields) -> Persona:
    pid, t = new_id(), now()
    try:
        await conn.execute(
            "insert into personas(id,name,description,system_prompt,default_mode,provider,model,tools_json,"
            "collections_json,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, fields["name"], fields.get("description") or "", fields["system_prompt"], fields.get("default_mode"),
             fields.get("provider"), fields.get("model"), json.dumps(fields.get("tools") or []),
             json.dumps(fields.get("collections") or []), t, t))
    except sqlite3.IntegrityError:
        await conn.rollback()
        raise ValueError("name taken") from None
    await conn.commit()
    return await get(conn, pid)


async def update(conn, pid: str, **fields) -> Persona:
    try:
        for k, v in fields.items():
            col = _COLUMNS[k]
            await conn.execute(f"update personas set {col}=? where id=?",
                               (json.dumps(v or []) if col.endswith("_json") else v, pid))
        await conn.execute("update personas set updated_at=? where id=?", (now(), pid))
    except sqlite3.IntegrityError:
        await conn.rollback()
        raise ValueError("name taken") from None
    await conn.commit()
    return await get(conn, pid)


async def delete(conn, pid: str) -> None:
    await conn.execute("update chats set persona_id=null where persona_id=?", (pid,))
    await conn.execute("delete from personas where id=?", (pid,))
    await conn.commit()
