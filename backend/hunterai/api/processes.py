"""The owner acting on processes directly (not tool calls)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import procs
from ..tools.base import ToolError
from .deps import get_conn, not_found

router = APIRouter(prefix="/api")


class InputIn(BaseModel):
    text: str
    eof: bool = False


async def _proc(pid: str) -> procs.Proc:
    p = await procs.get(pid)
    if p is None:
        raise not_found("process")
    return p


@router.get("/chats/{cid}/processes")
async def list_processes(cid: str, conn=Depends(get_conn)):
    return await procs.list_for_chat(conn, cid)


@router.get("/processes/{pid}/output")
async def process_output(pid: str, offset: int = 0, limit: int = procs.READ_BYTES):
    p = await _proc(pid)
    text, nxt, total = procs.read(p.log_path, max(0, offset), max(1, min(limit, 200_000)))
    return {"text": text, "next_offset": nxt, "total": total, "status": p.status,
            "exit_code": p.exit_code}


@router.post("/processes/{pid}/input")
async def process_input(pid: str, body: InputIn):
    await _proc(pid)
    try:
        await procs.send_input(pid, body.text, body.eof)
    except ToolError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@router.post("/processes/{pid}/kill")
async def process_kill(pid: str):
    p = await _proc(pid)
    try:
        return await procs.kill(pid)
    except ToolError:
        return p      # already ended, or started by an earlier app instance
