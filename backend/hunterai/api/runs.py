from __future__ import annotations

import json

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import engine, hub, permissions, savepoint
from ..repo import messages, runs
from .deps import get_conn, not_found

router = APIRouter(prefix="/api")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/runs/{rid}/events")
async def run_events(rid: str, conn=Depends(get_conn)):
    run = await runs.get(conn, rid)
    if not run:
        raise not_found("run")
    if hub.known(rid):
        async def live():
            async for event in hub.subscribe(rid):
                yield _sse(event)
        gen = live()
    else:
        # The run ended long ago (or before a restart): replay its outcome from the database.
        msg = await messages.get(conn, run.assistant_message_id) if run.assistant_message_id \
            else None
        replay = [
            {"type": "run_started", "run_id": rid,
             "assistant_message_id": run.assistant_message_id},
            {"type": "run_finished", "status": run.status, "error": run.error,
             "totals": run.totals, "message": msg.model_dump() if msg else None},
        ]

        async def stored():
            for event in replay:
                yield _sse(event)
        gen = stored()
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/runs/{rid}/cancel")
async def cancel_run(rid: str):
    return {"cancelled": await engine.cancel(rid)}


NOTHING_TO_UNDO = "This run changed no file in a folder with a save point, so there is nothing to put back."
ALREADY_UNDONE = "The changes of this run were already put back."
STILL_GOING = "A run of this chat is still going: wait for it to end, or stop it, then undo."


@router.post("/runs/{rid}/undo")
async def undo_run(rid: str, conn=Depends(get_conn)):
    """Put the files this run changed back to how they were before it. File by file: a file the owner changed
    since the run is left as it is and named, and nothing outside the run's own list is touched. It can be
    asked again until no file is left."""
    run = await runs.get(conn, rid)
    if not run:
        raise not_found("run")
    msg = await messages.get(conn, run.assistant_message_id) if run.assistant_message_id else None
    at = next((i for i, b in enumerate(msg.blocks) if b.type == "changes"), None) if msg else None
    if at is None or not msg.blocks[at].files:
        raise HTTPException(404, NOTHING_TO_UNDO)
    block = msg.blocks[at]
    if block.undone:
        raise HTTPException(409, ALREADY_UNDONE)
    if await runs.has_running(conn, run.chat_id):
        raise HTTPException(409, STILL_GOING)
    try:
        done = await savepoint.undo(block.folder, block.before, block.after)
    except savepoint.SavePointError as e:
        raise HTTPException(409, str(e)) from None
    blocks = list(msg.blocks)
    # Undone means nothing is left to do. While files were left (the owner's later work is in them), the list
    # keeps its button: asking again goes on with whatever is as the run left it by then.
    blocks[at] = block.model_copy(update={"undone": not done["left"], "left": done["left"]})
    msg = await messages.update(conn, msg.id, blocks=blocks)
    return {"message": msg, "restored": done["restored"], "left": done["left"]}


class PermissionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["allow_once", "allow_chat", "deny"]
    note: str = Field(default="", max_length=2000)


@router.get("/runs/{rid}/permissions")
async def pending_permissions(rid: str):
    """The asks of this run that still wait for the owner."""
    return permissions.pending(rid)


@router.post("/runs/{rid}/permissions/{request_id}")
async def answer_permission(rid: str, request_id: str, body: PermissionAnswer):
    # The only place a permission request is answered: the owner, through this route.
    req = permissions.get(rid, request_id)
    if req is None:
        raise HTTPException(404, "no such pending permission request")
    if body.decision == "allow_chat" and not req.can_allow_chat:
        raise HTTPException(400, "this call cannot be allowed for the whole chat")
    if not permissions.answer(rid, request_id, permissions.Answer(body.decision, body.note)):
        raise HTTPException(404, "no such pending permission request")
    return {"ok": True}


@router.get("/runs/{rid}")
async def get_run(rid: str, conn=Depends(get_conn)):
    run = await runs.get(conn, rid)
    if not run:
        raise not_found("run")
    return run


@router.get("/chats/{cid}/runs")
async def list_runs(cid: str, conn=Depends(get_conn)):
    return await runs.list_for_chat(conn, cid)
