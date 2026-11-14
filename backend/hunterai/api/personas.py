from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..repo import kb, personas
from ..tools import registry
from .deps import get_conn, not_found

router = APIRouter(prefix="/api/personas")
MAX_PROMPT = 20_000


class PersonaIn(BaseModel):
    name: str | None = Field(None, max_length=60)
    description: str | None = Field(None, max_length=300)
    system_prompt: str | None = Field(None, max_length=MAX_PROMPT)
    default_mode: Literal["plan", "pentesting", "coding"] | None = None
    provider: str | None = None
    model: str | None = None
    tools: list[str] | None = None
    collections: list[str] | None = None


async def _clean(conn, body: PersonaIn, *, creating: bool) -> dict:
    fields = body.model_dump(exclude_unset=True)
    for key in ("name", "system_prompt"):
        if key in fields or creating:
            fields[key] = (fields.get(key) or "").strip()
            if not fields[key]:
                raise HTTPException(400, f"a persona needs a {key.replace('_', ' ')}")
    if "tools" in fields:
        fields["tools"] = sorted({t for t in fields["tools"] or [] if isinstance(t, str) and t})[:300]
    if "collections" in fields:
        from ..repo import settings
        for cid in fields["collections"] or []:
            if await kb.get_collection(conn, cid) is None or cid == await settings.get(conn, "memory_collection_id"):
                raise not_found("collection")
    if fields.get("provider") == "":
        fields["provider"] = None
    if fields.get("model") == "":
        fields["model"] = None
    return fields


@router.get("")
async def list_personas(conn=Depends(get_conn)):
    return await personas.list(conn)


@router.post("")
async def create_persona(body: PersonaIn, conn=Depends(get_conn)):
    try:
        return await personas.create(conn, **await _clean(conn, body, creating=True))
    except ValueError:
        raise HTTPException(409, "a persona with this name exists")


@router.patch("/{pid}")
async def change_persona(pid: str, body: PersonaIn, conn=Depends(get_conn)):
    if not await personas.get(conn, pid):
        raise not_found("persona")
    try:
        return await personas.update(conn, pid, **await _clean(conn, body, creating=False))
    except ValueError:
        raise HTTPException(409, "a persona with this name exists")


@router.delete("/{pid}", status_code=204)
async def delete_persona(pid: str, conn=Depends(get_conn)):
    if not await personas.get(conn, pid):
        raise not_found("persona")
    await personas.delete(conn, pid)
    return Response(status_code=204)


@router.get("/tools")
async def tool_names():
    """Names a persona's tool list can hold."""
    return sorted(t.name for t in registry.all() if t.expose and (t.available is None or t.available()))
