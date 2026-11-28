from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from .. import notify
from ..repo import chats, findings, goals
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


class OwnerItems(BaseModel):
    items: list[str]


class OwnerItem(BaseModel):
    item: str


@router.post("/chats/{cid}/goal/items")
async def add_goal_items(cid: str, body: OwnerItems, conn=Depends(get_conn)):
    """The owner writes items onto the chat's goal record, each in his own words. They stand among its open items,
    and only he takes one off (repo/goals.py). Spacing inside an item counts as one space: an item is one line."""
    if not await chats.get(conn, cid):
        raise not_found("chat")
    items = list(dict.fromkeys(i for i in (" ".join(x.split()) for x in body.items) if i))
    if not items:
        raise HTTPException(400, "write at least one item")
    long = next((i for i in items if len(i) > goals.MAX_ITEM), None)
    if long:
        raise HTTPException(400, f"an item has at most {goals.MAX_ITEM} characters; this one has {len(long)}: {long[:60]}…")
    goal = await goals.get(conn, cid)
    have = goal.owner_items if goal else []
    new = [i for i in items if i not in have]
    if len(have) + len(new) > goals.MAX_OWNER_ITEMS:
        raise HTTPException(400, f"a chat's checklist holds {goals.MAX_OWNER_ITEMS} items: it has {len(have)}, "
                                 f"and this would add {len(new)}")
    return await goals.add_owner_items(conn, cid, items)


@router.post("/chats/{cid}/goal/items/remove")
async def remove_goal_item(cid: str, body: OwnerItem, conn=Depends(get_conn)):
    """The owner takes one of his items off. Gives the record as it then stands: null when it went with the item."""
    goal = await goals.get(conn, cid)
    if goal is None or body.item not in goal.owner_items:
        raise not_found("item")
    return await goals.remove_owner_item(conn, cid, body.item)


@router.post("/notify/test")
async def notify_test():
    """Send one line to the Discord webhook, so the owner sees that it works."""
    said = await notify.test_message()
    return {"ok": said == "sent", "detail": said}
