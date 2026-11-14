from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from .. import notify
from ..repo import findings, goals
from .deps import get_conn, not_found

router = APIRouter(prefix="/api")


class FindingPatch(BaseModel):
    title: str | None = None
    severity: Literal["info", "low", "medium", "high", "critical"] | None = None
    status: Literal["open", "confirmed", "false_positive", "duplicate", "reported", "fixed"] | None = None
    evidence: str | None = None


@router.get("/findings")
async def all_findings(conn=Depends(get_conn)):
    return await findings.list_all(conn)


@router.patch("/findings/{fid}")
async def patch_finding(fid: str, body: FindingPatch, conn=Depends(get_conn)):
    if not await findings.get(conn, fid):
        raise not_found("finding")
    updated = await findings.update(conn, fid, **body.model_dump(exclude_unset=True))
    await notify.finding(conn, fid)               # confirmed by the owner: announced, once
    return updated


@router.delete("/findings/{fid}", status_code=204)
async def delete_finding(fid: str, conn=Depends(get_conn)):
    if not await findings.get(conn, fid):
        raise not_found("finding")
    await findings.delete(conn, fid)
    return Response(status_code=204)


@router.get("/chats/{cid}/goal")
async def get_goal(cid: str, conn=Depends(get_conn)):
    """The chat's goal record, or null when none has been set."""
    return await goals.get(conn, cid)


@router.delete("/chats/{cid}/goal", status_code=204)
async def clear_goal(cid: str, conn=Depends(get_conn)):
    await goals.delete(conn, cid)
    return Response(status_code=204)


@router.post("/notify/test")
async def notify_test():
    """Send one line to the Discord webhook, so the owner sees that it works."""
    said = await notify.test_message()
    return {"ok": said == "sent", "detail": said}
