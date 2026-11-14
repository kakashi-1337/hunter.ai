from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .. import memory
from ..kb.embed import EmbedError
from ..kb.vectors import VectorError
from ..repo import chats, jobs, settings
from .deps import get_conn, not_found

router = APIRouter(prefix="/api")


class MemoryIn(BaseModel):
    text: str = Field(max_length=2000)
    kind: Literal["fact", "lesson", "preference"] = "fact"
    target_id: str | None = None


class MemoryPatch(BaseModel):
    text: str = Field(max_length=2000)


class MemorySettings(BaseModel):
    enabled: bool | None = None
    recall: bool | None = None


async def _guard(call):
    try:
        return await call
    except ValueError as e:
        raise HTTPException(400, str(e))
    except EmbedError as e:
        raise HTTPException(503, e.message)
    except VectorError as e:
        raise HTTPException(503, str(e))


async def _settings(conn) -> dict:
    col = await memory.collection(conn)
    return {"enabled": await memory.enabled(conn), "recall": await memory.recall_enabled(conn),
            "collection_id": col.id if col else None, "idle_seconds": memory.IDLE_SECONDS}


@router.get("/memories")
async def list_memories(target_id: str | None = None, limit: int = 200, offset: int = 0, conn=Depends(get_conn)):
    total, items = await memory.list_all(conn, target_id, max(1, min(limit, 500)), max(0, offset))
    return {"total": total, "items": items, "settings": await _settings(conn)}


@router.post("/memories")
async def add_memory(body: MemoryIn, conn=Depends(get_conn)):
    made = await _guard(memory.add(conn, body.text, body.kind, 0.7, target_id=body.target_id))
    if made is None:
        raise HTTPException(409, "this is already remembered")
    return made


@router.patch("/memories/{mid}")
async def change_memory(mid: str, body: MemoryPatch, conn=Depends(get_conn)):
    try:
        return await _guard(memory.change(conn, mid, body.text))
    except KeyError:
        raise not_found("memory")


@router.delete("/memories/{mid}", status_code=204)
async def delete_memory(mid: str, conn=Depends(get_conn)):
    if await memory.get(conn, mid) is None:
        raise not_found("memory")
    await _guard(memory.remove(conn, mid))
    return Response(status_code=204)


@router.put("/memories/settings")
async def put_settings(body: MemorySettings, conn=Depends(get_conn)):
    if body.enabled is not None:
        await settings.put(conn, "memory_enabled", body.enabled)
    if body.recall is not None:
        await settings.put(conn, "memory_recall", body.recall)
    return await _settings(conn)


@router.post("/chats/{cid}/memorize")
async def memorize(cid: str, conn=Depends(get_conn)):
    """Queue this chat's notes now instead of waiting for it to go quiet."""
    if not await chats.get(conn, cid):
        raise not_found("chat")
    waiting = await jobs.list(conn, kind=memory.KIND, states=["queued", "running"], payload_eq={"chat_id": cid}, limit=1)
    return waiting[0] if waiting else await jobs.enqueue(conn, memory.KIND, {"chat_id": cid})
