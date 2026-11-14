from __future__ import annotations

import asyncio
import os
import shutil
import weakref
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, ValidationError

from .. import config, engine, procs, uploads
from ..permissions import Policy
from ..repo import chats, grants, messages, runs, targets
from ..repo import personas
from ..tools import resolve
from ..types import Block, Mode, TextBlock
from .deps import default_provider_model, get_conn, load_upload_bytes, not_found
from ..db import connect

router = APIRouter(prefix="/api")


class ChatIn(BaseModel):
    target_id: str | None = None
    mode: Mode = "pentesting"
    provider: str | None = None
    model: str | None = None
    options: dict = {}
    persona_id: str | None = None


class ChatPatch(BaseModel):
    persona_id: str | None = None
    title: str | None = None
    mode: Mode | None = None
    provider: str | None = None
    model: str | None = None
    options: dict | None = None
    target_id: str | None = None
    permissions: dict | None = None
    workdir: str | None = None


def _workdir_of(chat) -> str:
    """The chat's working folder, without creating it."""
    return chat.workdir or str(config.home() / "work" / chat.id)


def _checked_workdir(raw: str) -> str:
    """A custom working folder: absolute, real, not a folder that holds everything else."""
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise HTTPException(400, "the working folder must be an absolute path")
    path = Path(os.path.realpath(expanded))
    for anchor in (Path.home(), config.home()):
        if resolve.is_inside(anchor, path):       # the anchor itself, or one of its ancestors
            raise HTTPException(400, f"{path} is too broad to be a working folder")
    if resolve.is_inside(path, config.home()) and not resolve.is_inside(path, config.home() / "work"):
        raise HTTPException(400, f"{path} is part of HunterAI's own data")
    why = resolve.protected(path, "write")
    if why:
        raise HTTPException(400, why)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"cannot create {path}: {e.strerror}")
    return str(path)


class SendIn(BaseModel):
    text: str = ""
    upload_ids: list[str] = []


class EditIn(BaseModel):
    text: str = ""
    upload_ids: list[str] | None = None


class LeafIn(BaseModel):
    message_id: str


async def _attachments(conn, upload_ids: list[str]) -> list[Block]:
    out = []
    for uid in upload_ids:
        try:
            out.append(await uploads.to_block(conn, uid))
        except KeyError:
            raise HTTPException(400, f"unknown upload: {uid}")
    return out


# One lock per chat, held from the "is a run in progress" check until the new run exists,
# so concurrent requests cannot all pass the check. Entries vanish when nobody holds them.
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()


def _chat_lock(chat_id: str) -> asyncio.Lock:
    lock = _locks.get(chat_id)
    if lock is None:
        lock = _locks[chat_id] = asyncio.Lock()
    return lock


async def _no_running(conn, chat_id: str) -> None:
    """Call with the chat's lock held. A 'running' row the engine no longer tracks is a run
    that could not record its end; it must not block the chat (startup recovery closes it)."""
    if any(engine.is_active(rid) for rid in await runs.running_ids(conn, chat_id)):
        raise HTTPException(409, "a run is already in progress for this chat")


async def _start(chat_id: str, user_message_id: str) -> dict:
    run_id, assistant_id = await engine.start_run(connect, chat_id, user_message_id,
                                                  load_upload_bytes)
    return {"run_id": run_id, "user_message_id": user_message_id,
            "assistant_message_id": assistant_id}


@router.get("/chats")
async def list_chats(target_id: str | None = None, conn=Depends(get_conn)):
    return await chats.list(conn, target_id)


@router.post("/chats")
async def create_chat(body: ChatIn, conn=Depends(get_conn)):
    if body.target_id and not await targets.get(conn, body.target_id):
        raise not_found("target")
    provider, model = await default_provider_model(conn)
    persona = await personas.get(conn, body.persona_id)
    if body.persona_id and persona is None:
        raise not_found("persona")
    if persona is not None and persona.provider and persona.model:
        provider, model = persona.provider, persona.model       # the persona's preferred model
    if body.provider:
        provider, model = body.provider, body.model or model
    elif body.model:
        model = body.model
    chat = await chats.create(conn, body.target_id, body.mode, provider, model, body.options,
                              persona.id if persona else None)
    await _persona_collections(conn, chat.id, persona)
    return chat


async def _persona_collections(conn, chat_id: str, persona) -> None:
    """A persona's default knowledge collections, for a chat that has none attached yet."""
    if persona is None or not persona.collections:
        return
    from ..repo import kb
    if (await kb.get_chat_kb(conn, chat_id)).collection_ids:
        return
    known = [c for c in persona.collections if await kb.get_collection(conn, c)]
    if known:
        await kb.set_chat_kb(conn, chat_id, collection_ids=known)


@router.get("/chats/{cid}")
async def get_chat(cid: str, conn=Depends(get_conn)):
    chat = await chats.get(conn, cid)
    if not chat:
        raise not_found("chat")
    out = []
    for m in await messages.path(conn, cid):
        d = m.model_dump()
        d["siblings"] = await messages.siblings(conn, m.id)
        out.append(d)
    return {"chat": chat, "messages": out, "workdir": _workdir_of(chat)}


@router.patch("/chats/{cid}")
async def patch_chat(cid: str, body: ChatPatch, conn=Depends(get_conn)):
    chat = await chats.get(conn, cid)
    if not chat:
        raise not_found("chat")
    fields = body.model_dump(exclude_unset=True)
    if fields.get("target_id") and not await targets.get(conn, fields["target_id"]):
        raise not_found("target")
    for key in ("title", "mode", "provider", "model"):
        if key in fields and not fields[key]:
            fields.pop(key)
    persona = None
    if fields.get("persona_id"):
        persona = await personas.get(conn, fields["persona_id"])
        if persona is None:
            raise not_found("persona")
    elif "persona_id" in fields:
        fields["persona_id"] = None
    if "permissions" in fields:
        try:
            fields["permissions"] = Policy(**(fields["permissions"] or {})).model_dump(
                exclude_defaults=True)
        except (ValidationError, TypeError) as e:
            raise HTTPException(400, f"invalid permissions: {str(e).splitlines()[0]}")
    if "workdir" in fields:
        fields["workdir"] = _checked_workdir(fields["workdir"]) if fields["workdir"] else None
    changed = any(k in fields and fields[k] != getattr(chat, k) for k in ("mode", "target_id"))
    updated = await chats.update(conn, cid, **fields)
    await _persona_collections(conn, cid, persona)
    if changed:
        # An "allow for this chat" answer was given for another mode or target.
        await grants.clear(conn, cid)
    return updated


@router.get("/chats/{cid}/grants")
async def list_grants(cid: str, conn=Depends(get_conn)):
    return [{"tool": g.tool, "target": g.target, "warned": g.warned}
            for g in await grants.list(conn, cid)]


@router.delete("/chats/{cid}/grants", status_code=204)
async def clear_grants(cid: str, conn=Depends(get_conn)):
    await grants.clear(conn, cid)
    return Response(status_code=204)


@router.delete("/chats/{cid}", status_code=204)
async def delete_chat(cid: str, conn=Depends(get_conn)):
    # The id becomes a folder name below, so it must be a chat that exists ("." or ".."
    # sent by a non-browser client would name the whole work or data folder).
    if not await chats.get(conn, cid):
        raise not_found("chat")
    for r in await runs.list_for_chat(conn, cid):
        if r.status == "running":
            await engine.cancel(r.id)
            await engine.wait(r.id)
    # The chat's processes die with it, and so do the files HunterAI made for it. A custom
    # working folder is the owner's and is never deleted.
    await procs.kill_chat(cid)
    logs = [p.log_path for p in await procs.list_for_chat(conn, cid)]
    run_ids = [r.id for r in await runs.list_for_chat(conn, cid)]
    await chats.delete(conn, cid)
    from .. import browser
    await browser.close(cid)          # its browser session, cookies and all, goes with it
    shutil.rmtree(config.home() / "work" / cid, ignore_errors=True)
    for log in logs:
        try:
            os.unlink(log)
        except OSError:
            pass
    for rid in run_ids:
        shutil.rmtree(config.output_dir() / rid, ignore_errors=True)
    return Response(status_code=204)


@router.post("/chats/{cid}/messages")
async def send_message(cid: str, body: SendIn, conn=Depends(get_conn)):
    chat = await chats.get(conn, cid)
    if not chat:
        raise not_found("chat")
    if not body.text.strip() and not body.upload_ids:
        raise HTTPException(400, "message is empty")
    async with _chat_lock(cid):
        await _no_running(conn, cid)
        chat = await chats.get(conn, cid)  # the leaf may have moved while waiting for the lock
        blocks: list[Block] = [TextBlock(text=body.text)] if body.text.strip() else []
        blocks += await _attachments(conn, body.upload_ids)
        user = await messages.add(conn, cid, chat.active_leaf_id, "user", blocks)
        return await _start(cid, user.id)


@router.post("/chats/{cid}/active-leaf")
async def set_active_leaf(cid: str, body: LeafIn, conn=Depends(get_conn)):
    m = await messages.get(conn, body.message_id)
    if not m or m.chat_id != cid:
        raise not_found("message")
    await messages.set_active(conn, cid, m.id)
    return {"ok": True}


@router.post("/messages/{mid}/edit")
async def edit_message(mid: str, body: EditIn, conn=Depends(get_conn)):
    m = await messages.get(conn, mid)
    if not m:
        raise not_found("message")
    if m.role != "user":
        raise HTTPException(400, "only user messages can be edited")
    if body.upload_ids is None:
        extras = [b for b in m.blocks if b.type in ("image", "file")]
    else:
        extras = await _attachments(conn, body.upload_ids)
    if not body.text.strip() and not extras:
        raise HTTPException(400, "message is empty")
    async with _chat_lock(m.chat_id):
        await _no_running(conn, m.chat_id)
        blocks: list[Block] = ([TextBlock(text=body.text)] if body.text.strip() else []) + extras
        user = await messages.add(conn, m.chat_id, m.parent_id, "user", blocks)
        return await _start(m.chat_id, user.id)


@router.post("/messages/{mid}/retry")
async def retry_message(mid: str, conn=Depends(get_conn)):
    m = await messages.get(conn, mid)
    if not m:
        raise not_found("message")
    if m.role != "assistant" or not m.parent_id:
        raise HTTPException(400, "only assistant messages can be retried")
    async with _chat_lock(m.chat_id):
        await _no_running(conn, m.chat_id)
        return await _start(m.chat_id, m.parent_id)


@router.delete("/messages/{mid}", status_code=204)
async def delete_message(mid: str, conn=Depends(get_conn)):
    m = await messages.get(conn, mid)
    if not m:
        raise not_found("message")
    async with _chat_lock(m.chat_id):
        await _no_running(conn, m.chat_id)
        await messages.delete(conn, mid)
    return Response(status_code=204)


# ---- messages sent while a run is in progress

class QueueIn(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


async def _chat_or_404(conn, cid: str):
    chat = await chats.get(conn, cid)
    if chat is None:
        raise not_found("chat")
    return chat


@router.get("/chats/{cid}/queue")
async def list_queue(cid: str, conn=Depends(get_conn)):
    from ..repo import queue
    await _chat_or_404(conn, cid)
    return await queue.list(conn, cid)


@router.post("/chats/{cid}/queue")
async def add_to_queue(cid: str, body: QueueIn, conn=Depends(get_conn)):
    """Keep a message for the agent's next step. Only while a run is in progress: otherwise it is a normal message."""
    from ..repo import queue, runs
    await _chat_or_404(conn, cid)
    if not body.text.strip():
        raise HTTPException(422, "the message is empty")
    if not await runs.has_running(conn, cid):
        raise HTTPException(409, "no run is in progress in this chat: send it as a message")
    return await queue.add(conn, cid, body.text)


@router.delete("/chats/{cid}/queue/{qid}", status_code=204)
async def remove_from_queue(cid: str, qid: str, conn=Depends(get_conn)):
    from ..repo import queue
    if not await queue.delete(conn, cid, qid):
        raise not_found("queued message")
    return Response(status_code=204)


@router.post("/chats/{cid}/queue/take")
async def take_queue(cid: str, conn=Depends(get_conn)):
    """What the run did not get to read, removed from the queue: the interface sends it as the next message."""
    from ..repo import queue
    await _chat_or_404(conn, cid)
    return await queue.take(conn, cid)
