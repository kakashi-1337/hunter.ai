"""Knowledge base: collections, ingest (upload or a folder on this machine), jobs, history,
status, search, and which collections a chat reads. The worker does the ingest work; this
API only queues it and reports on it."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import config, worker
from ..kb import embed, gone, limits, search, sources
from ..kb.embed import EmbedError
from ..kb.vectors import VectorError, get_store
from ..repo import chats, jobs, kb
from .deps import get_conn, not_found

router = APIRouter(prefix="/api/kb")
ACTIVE = ["queued", "running", "paused"]
KINDS = ("ingest", "reembed")          # the job kinds this API shows and controls
MAX_NOTE_CHARS = 200_000
MAX_FILES_PER_REQUEST = 100


class CollectionIn(BaseModel):
    name: str
    embedder: str | None = None


class RenameIn(BaseModel):
    name: str


class FolderIn(BaseModel):
    path: str
    again: bool = False           # ingest it although this collection already has it (to refresh it)


class GitIn(BaseModel):
    url: str
    again: bool = False


class WebIn(BaseModel):
    url: str
    max_pages: int = Field(200, ge=1, le=5000)
    max_depth: int = Field(2, ge=0, le=10)
    robots: bool = True
    allow_local: bool = False     # the owner says this site is on this machine or network
    render: bool = False          # open pages that are empty without JavaScript in the browser
    again: bool = False


class ChunkIn(BaseModel):
    text: str


class NoteIn(BaseModel):
    title: str
    text: str


class ReembedIn(BaseModel):
    embedder: str


class SearchIn(BaseModel):
    collection_ids: list[str]
    query: str
    limit: int = Field(8, ge=1, le=50)


class ChatKbIn(BaseModel):
    collection_ids: list[str] | None = None
    always_retrieve: bool | None = None


def _name(raw: str) -> str:
    name = (raw or "").strip()
    if not 1 <= len(name) <= 80:
        raise HTTPException(400, "a collection name has 1 to 80 characters")
    return name


async def _active(conn, cid: str) -> list:
    found = await jobs.list(conn, states=ACTIVE, payload_eq={"collection_id": cid}, limit=None)
    return [j for j in found if j.kind in KINDS]


async def _memory_id(conn) -> str | None:
    from ..repo import settings
    return await settings.get(conn, "memory_collection_id")


async def _collection(conn, cid: str):
    """A knowledge collection the owner manages here. The memory collection is not one of
    them: it is managed under Settings, Memory, and cannot be attached, ingested into, or deleted here."""
    col = await kb.get_collection(conn, cid)
    if col is None or cid == await _memory_id(conn):
        raise not_found("collection")
    return col


@router.get("/health")
async def health(conn=Depends(get_conn)):
    return {"worker_alive": await worker.alive(conn), "vector_backend": config.vector_backend()}


@router.get("/collections")
async def list_collections(conn=Depends(get_conn)):
    hidden = await _memory_id(conn)
    return [c for c in await kb.list_collections(conn) if c.id != hidden]


@router.post("/collections")
async def create_collection(body: CollectionIn, conn=Depends(get_conn)):
    name = _name(body.name)
    spec = body.embedder or embed.DEFAULT_EMBEDDER
    try:
        embedder = embed.get(spec, lock_timeout=30)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        dimension = await embed.dimension(embedder)
    except EmbedError as e:
        raise HTTPException(503 if e.kind == "unavailable" else 400, e.message)
    try:
        col = await kb.create_collection(conn, name, spec, dimension)
    except ValueError:
        raise HTTPException(409, "a collection with this name exists")
    try:
        await get_store().ensure_collection(kb.vector_name(col.id), dimension)
    except VectorError as e:
        await kb.delete_collection(conn, col.id)      # nothing is left behind
        raise HTTPException(503, str(e))
    return col


@router.patch("/collections/{cid}")
async def rename_collection(cid: str, body: RenameIn, conn=Depends(get_conn)):
    await _collection(conn, cid)
    try:
        return await kb.rename_collection(conn, cid, _name(body.name))
    except ValueError:
        raise HTTPException(409, "a collection with this name exists")


@router.delete("/collections/{cid}", status_code=204)
async def delete_collection(cid: str, conn=Depends(get_conn)):
    await _collection(conn, cid)
    if await _active(conn, cid):
        raise HTTPException(409, "cancel its jobs first")
    for job in await jobs.list(conn, kind="ingest", states=["failed"], payload_eq={"collection_id": cid}, limit=None):
        await jobs.request(conn, job.id, "cancel")     # the worker's cleanup removes their staging
    try:
        await get_store().drop(kb.vector_name(cid))
    except VectorError as e:
        raise HTTPException(503, str(e))
    await kb.delete_collection(conn, cid)
    return Response(status_code=204)


@router.get("/collections/{cid}/status")
async def status(cid: str, conn=Depends(get_conn)):
    col = await _collection(conn, cid)
    counts = await kb.counts(conn, cid)
    vectors, vector_error = None, None
    try:
        vectors = await get_store().count(kb.vector_name(cid))
    except VectorError as e:
        vector_error = str(e)
    active = await _active(conn, cid)
    job = next((j for j in active if j.state == "running"), active[0] if active else None)
    running = job is not None and job.state == "running"
    return {"collection": col, **counts, "vectors": vectors, "vector_error": vector_error,
            "dimension": col.dimension, "embedder": col.embedder, "active_job": job,
            "embed_rate": job.stats.get("embed_rate") if running else None,
            "eta_seconds": job.stats.get("eta_seconds") if running else None}


@router.get("/collections/{cid}/documents")
async def documents(cid: str, status: Literal["ok", "skipped", "failed", "processing"] | None = None,  # noqa: A002
                    limit: int = 100, offset: int = 0, conn=Depends(get_conn)):
    await _collection(conn, cid)
    total, items = await kb.list_documents(conn, cid, status, max(1, min(limit, 500)), max(0, offset))
    return {"total": total, "items": items}


# ---- uploads

def _staging(upload_id: str, *, uncommitted: bool = True):
    try:
        info = sources.staging_info(upload_id)
    except (ValueError, FileNotFoundError, OSError):
        raise not_found("upload")
    if uncommitted and info.job_id:
        raise HTTPException(409, "this upload is already committed")
    return info


@router.post("/collections/{cid}/uploads")
async def start_upload(cid: str, conn=Depends(get_conn)):
    await _collection(conn, cid)
    try:
        return {"upload_id": sources.new_staging(cid)}
    except limits.NoSpace as e:
        raise HTTPException(507, str(e))


@router.post("/uploads/{upload_id}/files")
async def upload_files(upload_id: str, request: Request):
    _staging(upload_id)
    # The multipart parser spools every part to disk before a handler sees it: the size and
    # the disk floor are checked on the declared length first.
    declared = request.headers.get("content-length")
    if not declared or not declared.isdigit() or int(declared) > limits.MAX_REQUEST_BYTES:
        raise HTTPException(413, f"an upload request may be at most {limits.MAX_REQUEST_BYTES} bytes")
    try:
        limits.require_space(int(declared))
    except limits.NoSpace as e:
        raise HTTPException(507, str(e))
    async with request.form(max_files=MAX_FILES_PER_REQUEST + 1, max_fields=4 * MAX_FILES_PER_REQUEST + 8) as form:
        files, paths = form.getlist("files"), [str(p) for p in form.getlist("paths")]
        if len(files) > MAX_FILES_PER_REQUEST:
            raise HTTPException(400, f"at most {MAX_FILES_PER_REQUEST} files per request")
        if len(files) != len(paths):
            raise HTTPException(400, "files and paths must have the same length")
        accepted, rejected = 0, []
        for f, path in zip(files, paths):
            staged = await sources.stage_file(upload_id, path, f.read)
            if staged.rejected:
                rejected.append({"path": path, "reason": staged.rejected})
            else:
                accepted += 1
        for path in form.getlist("too_large"):
            reason = f"file too large (over {limits.MAX_FILE_BYTES} bytes)"
            await sources.reject(upload_id, str(path), reason)
            rejected.append({"path": str(path), "reason": reason})
    info = sources.staging_info(upload_id)
    return {"accepted": accepted, "rejected": rejected, "files": info.files, "bytes": info.bytes}


@router.post("/uploads/{upload_id}/commit")
async def commit_upload(upload_id: str, conn=Depends(get_conn)):
    info = _staging(upload_id)
    if info.files == 0:
        raise HTTPException(400, "no file was accepted for this upload")
    await _collection(conn, info.collection_id)
    job = await jobs.enqueue(conn, "ingest", {"collection_id": info.collection_id, "staging_id": upload_id,
                                              "source": {"kind": "upload"}})
    sources.mark_committed(upload_id, job.id)
    return job


@router.delete("/uploads/{upload_id}", status_code=204)
async def cancel_upload(upload_id: str):
    _staging(upload_id)
    sources.remove_staging(upload_id)
    return Response(status_code=204)


@router.post("/collections/{cid}/ingest/folder")
async def ingest_folder(cid: str, body: FolderIn, conn=Depends(get_conn)):
    await _collection(conn, cid)
    raw = Path(body.path)
    if not raw.is_absolute():
        raise HTTPException(400, "the path must be absolute")
    try:
        path = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, "the path does not exist")
    if not (path.is_dir() or path.is_file()):
        raise HTTPException(400, "the path must be a folder or a file")
    home = config.home().resolve()
    if path == home or home in path.parents:
        raise HTTPException(400, "HunterAI's own data folder cannot be ingested")
    await _not_twice(conn, cid, {"kind": "folder", "path": str(path)}, body.again)
    try:
        sid = sources.new_staging(cid)
    except limits.NoSpace as e:
        raise HTTPException(507, str(e))
    job = await jobs.enqueue(conn, "ingest", {"collection_id": cid, "staging_id": sid,
                                              "source": {"kind": "folder", "path": str(path)}})
    sources.mark_committed(sid, job.id)
    return job


def _where(source: dict) -> str:
    return str(source.get("url") or source.get("path") or "")


async def _not_twice(conn, cid: str, source: dict, again: bool, known_prefix: str | None = None) -> None:
    """Refuse a source this collection is already taking in, and one it already has unless the
    owner asked for it again. 409 with a sentence the interface shows as it is."""
    key = "url" if "url" in source else "path"
    same = [j for j in await jobs.list(conn, kind="ingest", limit=None, payload_eq={
        "collection_id": cid, "source.kind": source["kind"], f"source.{key}": source[key]})]
    waiting = next((j for j in same if j.state in ACTIVE), None)
    if waiting is not None:
        raise HTTPException(409, f"{_where(source)} is already in the queue of this collection ({waiting.state}): "
                                 "wait for it, or cancel it first")
    if again:
        return
    def files(j) -> int:
        return int((j.stats or {}).get("ok") or 0) + int((j.stats or {}).get("unchanged") or 0)
    done = next((j for j in same if j.state == "done" and files(j)), None)       # the newest first; a run that brought nothing in is not one
    if done is not None:
        n = files(done)
        raise HTTPException(409, f"Already ingested: {_where(source)} was added to this collection on "
                                 f"{str(done.finished_at or done.created_at)[:10]} ({n} file{'' if n == 1 else 's'}). "
                                 "Ingest it again only to pick up changes.")
    if known_prefix is not None:                                     # its queue entry was removed, its documents are here
        exact = source["kind"] == "web"
        r = await (await conn.execute(
            "select count(*), max(updated_at) from kb_documents where collection_id=? and source_kind=? and "
            + ("source_uri=?" if exact else "substr(source_uri, 1, ?)=?"),
            (cid, source["kind"], known_prefix) if exact else (cid, source["kind"], len(known_prefix), known_prefix))).fetchone()
        if r[0]:
            raise HTTPException(409, f"Already ingested: {_where(source)} was added to this collection on "
                                     f"{str(r[1])[:10]} ({r[0]} file{'' if r[0] == 1 else 's'}). "
                                     "Ingest it again only to pick up changes.")


async def _queue_remote(conn, cid: str, source: dict):
    await _collection(conn, cid)
    try:
        sid = sources.new_staging(cid)
    except limits.NoSpace as e:
        raise HTTPException(507, str(e))
    job = await jobs.enqueue(conn, "ingest", {"collection_id": cid, "staging_id": sid, "source": source})
    sources.mark_committed(sid, job.id)
    return job


@router.post("/collections/{cid}/ingest/git")
async def ingest_git(cid: str, body: GitIn, conn=Depends(get_conn)):
    """Queue a shallow clone of a public GitHub or GitLab repository."""
    from ..kb import gitsrc
    try:
        url, label = gitsrc.validate(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _collection(conn, cid)
    await _not_twice(conn, cid, {"kind": "git", "url": url}, body.again, f"git:{label}/")
    return await _queue_remote(conn, cid, {"kind": "git", "url": url})


@router.post("/collections/{cid}/ingest/web")
async def ingest_web(cid: str, body: WebIn, conn=Depends(get_conn)):
    """Queue a crawl that stays on the URL's host."""
    from ..kb import crawl
    try:
        url = crawl.validate(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _collection(conn, cid)
    await _not_twice(conn, cid, {"kind": "web", "url": url}, body.again, url)
    return await _queue_remote(conn, cid, {"kind": "web", "url": url, "max_pages": body.max_pages,
                                           "max_depth": body.max_depth, "robots": body.robots,
                                           "allow_local": body.allow_local, "render": body.render})


# ---- jobs

async def _job(conn, job_id: str):
    job = await jobs.get(conn, job_id)
    if job is None or job.kind not in KINDS:
        raise not_found("job")
    return job


@router.get("/jobs")
async def list_jobs(collection_id: str | None = None, limit: int = 50, conn=Depends(get_conn)):
    found = await jobs.list(conn, limit=None, payload_eq={"collection_id": collection_id} if collection_id else None)
    return [j for j in found if j.kind in KINDS][:max(1, min(limit, 200))]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, conn=Depends(get_conn)):
    return await _job(conn, job_id)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str, conn=Depends(get_conn)):
    """Remove a finished entry from the queue. What it ingested stays in the collection."""
    job = await _job(conn, job_id)
    if job.state not in ("done", "cancelled"):
        raise HTTPException(409, f"a {job.state} job cannot be removed from the list" +
                            ("; cancel it first, which also frees its staged files" if job.state != "running" else "; cancel it first"))
    await jobs.delete(conn, job_id)
    return Response(status_code=204)


@router.post("/collections/{cid}/jobs/prune")
async def prune_jobs(cid: str, conn=Depends(get_conn)):
    """Remove the older finished entries of a source that was ingested more than once; the newest
    of each stays. Only entries of the list go: documents are untouched."""
    await _collection(conn, cid)
    kept, removed = set(), 0
    for j in await jobs.list(conn, kind="ingest", limit=None, payload_eq={"collection_id": cid}):     # the newest first
        source = j.payload.get("source") or {}
        key = (source.get("kind"), _where(source))
        if not key[1] or j.state not in ("done", "cancelled"):
            continue                                  # waiting, running, and failed entries are the owner's to deal with
        if key in kept:
            await jobs.delete(conn, j.id)
            removed += 1
        elif j.state == "done":
            kept.add(key)                             # the newest finished run of this source stays as its record
    return {"removed": removed}


@router.post("/jobs/{job_id}/{action}")
async def job_action(job_id: str, action: str, conn=Depends(get_conn)):
    job = await _job(conn, job_id)
    if action not in ("pause", "resume", "cancel", "retry"):
        raise HTTPException(400, "unknown action")
    if action == "cancel" and job.kind == "reembed" and job.checkpoint.get("phase") == "embed":
        # Its old vectors are already gone: stopping here would leave a half-empty index.
        raise HTTPException(409, "a re-embed that has started cannot be cancelled, only paused; "
                                 "let it finish, or start another re-embed afterwards")
    try:
        return await jobs.request(conn, job_id, action)
    except jobs.JobStateError as e:
        raise HTTPException(409, f"cannot {e.action} a {e.state} job")


# ---- index browser: documents, chunks, notes, re-embedding

async def _document(conn, did: str):
    doc = await kb.get_document(conn, did)
    if doc is None:
        raise not_found("document")
    return doc


async def _quiet(conn, cid: str) -> None:
    if await _active(conn, cid):
        raise HTTPException(409, "wait for this collection's jobs to finish, or cancel them")


async def _embed(col, texts: list[str]) -> list[list[float]]:
    """Embed in the API process: short waits only, so a busy worker gives a 503, not a hang."""
    out: list[list[float]] = []
    several = len(texts) > embed.EMBED_BATCH      # more than one request: the model is loaded once and unloaded once
    embedder = None
    try:
        embedder = embed.get(col.embedder, lock_timeout=30, stays_loaded=several)
        for i in range(0, len(texts), embed.EMBED_BATCH):
            out += await embedder.embed(texts[i:i + embed.EMBED_BATCH])
    except EmbedError as e:
        raise HTTPException(503 if e.kind == "unavailable" else 400, e.message)
    finally:
        if several and embedder is not None:
            await embedder.release()
    if any(len(v) != col.dimension for v in out):
        raise HTTPException(409, "the embedder's vector size no longer matches this collection; re-embed it")
    return out


def _gone(found: gone.Found) -> dict:
    return {"checked": found.checked, "gone": len(found.gone), "first": [uri for _, uri in found.gone[:gone.NAMED]],
            "away": found.away, "sources": found.sources, "unknown": found.unknown}


@router.get("/collections/{cid}/gone")
async def look_for_gone(cid: str, conn=Depends(get_conn)):
    """The documents of a collection that were read from a folder of this machine and whose file is gone from it:
    how many, and the first of them by name. Nothing is changed."""
    await _collection(conn, cid)
    return _gone(await gone.look(conn, cid))


@router.post("/collections/{cid}/gone/remove")
async def remove_gone(cid: str, conn=Depends(get_conn)):
    """Takes those documents out, with their passages. It looks again first: what is there now stays."""
    await _collection(conn, cid)
    await _quiet(conn, cid)
    found = await gone.look(conn, cid)
    store = get_store()
    for did, _ in found.gone:
        try:
            await store.delete_by_document(kb.vector_name(cid), did)
        except VectorError as e:
            raise HTTPException(503, str(e))
        await kb.delete_document(conn, did)
    return {"removed": len(found.gone)}


@router.get("/documents/{did}/chunks")
async def list_chunks(did: str, limit: int = 100, offset: int = 0, conn=Depends(get_conn)):
    doc = await _document(conn, did)
    total, items = await kb.list_chunks(conn, did, max(1, min(limit, 500)), max(0, offset))
    return {"document": doc, "total": total, "items": items}


@router.delete("/documents/{did}", status_code=204)
async def delete_document(did: str, conn=Depends(get_conn)):
    doc = await _document(conn, did)
    await _quiet(conn, doc.collection_id)
    try:
        await get_store().delete_by_document(kb.vector_name(doc.collection_id), did)
    except VectorError as e:
        raise HTTPException(503, str(e))
    await kb.delete_document(conn, did)
    return Response(status_code=204)


@router.patch("/chunks/{chunk_id}")
async def edit_chunk(chunk_id: str, body: ChunkIn, conn=Depends(get_conn)):
    """Change a chunk's text. Its vector is made again first; nothing changes if that fails."""
    from ..kb import chunking
    from ..kb.vectors import Point
    chunk = await kb.get_chunk(conn, chunk_id)
    if chunk is None:
        raise not_found("chunk")
    text = body.text.replace("\r\n", "\n").strip()
    if not text or len(text) > chunking.MAX_CHUNK_CHARS:
        raise HTTPException(400, f"a chunk has 1 to {chunking.MAX_CHUNK_CHARS} characters")
    await _quiet(conn, chunk.collection_id)
    col = await _collection(conn, chunk.collection_id)
    vector = (await _embed(col, [text]))[0]
    try:
        await get_store().upsert(kb.vector_name(col.id), [Point(chunk.id, chunk.document_id, vector)])
    except VectorError as e:
        raise HTTPException(503, str(e))
    return await kb.update_chunk_text(conn, chunk_id, text)


@router.delete("/chunks/{chunk_id}", status_code=204)
async def delete_chunk(chunk_id: str, conn=Depends(get_conn)):
    chunk = await kb.get_chunk(conn, chunk_id)
    if chunk is None:
        raise not_found("chunk")
    await _quiet(conn, chunk.collection_id)
    try:
        await get_store().delete(kb.vector_name(chunk.collection_id), [chunk_id])
    except VectorError as e:
        raise HTTPException(503, str(e))
    await kb.delete_chunk(conn, chunk_id)
    return Response(status_code=204)


@router.post("/collections/{cid}/notes")
async def add_note(cid: str, body: NoteIn, conn=Depends(get_conn)):
    """Add text by hand as its own document ("manual:<title>"); the same title replaces it."""
    import hashlib

    from ..kb import chunking
    from ..kb.vectors import Point
    col = await _collection(conn, cid)
    title = " ".join(body.title.split())[:120]
    text = body.text.replace("\r\n", "\n")
    if not title or not text.strip() or len(text) > MAX_NOTE_CHARS:
        raise HTTPException(400, f"a note needs a title and 1 to {MAX_NOTE_CHARS} characters of text")
    await _quiet(conn, cid)
    drafts = chunking.chunk(text, "markdown")
    vectors = await _embed(col, [d.text for d in drafts])
    store, name, uri = get_store(), kb.vector_name(cid), f"manual:{title}"
    try:
        old = await kb.find_document(conn, cid, uri)
        if old is not None:
            await store.delete_by_document(name, old.id)
        doc = await kb.begin_document(conn, cid, "manual", uri, hashlib.sha256(text.encode()).hexdigest(),
                                      len(text.encode()), None, drafts)
    except VectorError as e:
        raise HTTPException(503, str(e))
    try:
        await store.upsert(name, [Point(kb.chunk_id(doc.id, d.ordinal), doc.id, v) for d, v in zip(drafts, vectors)])
    except VectorError as e:
        # No job would ever finish a note left half-written: record it as failed instead.
        await kb.record_document(conn, cid, "manual", uri, status="failed", reason=f"vector store unavailable: {e}")
        raise HTTPException(503, str(e))
    await kb.mark_embedded(conn, [kb.chunk_id(doc.id, d.ordinal) for d in drafts])
    return await kb.complete_document(conn, doc.id)


@router.post("/collections/{cid}/reembed")
async def reembed(cid: str, body: ReembedIn, conn=Depends(get_conn)):
    """Queue embedding the whole collection again with another embedder."""
    await _collection(conn, cid)
    try:
        embed.get(body.embedder)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _quiet(conn, cid)
    return await jobs.enqueue(conn, "reembed", {"collection_id": cid, "embedder": body.embedder})


# ---- search and chat attachment

@router.post("/search")
async def run_search(body: SearchIn, conn=Depends(get_conn)):
    for cid in body.collection_ids:
        await _collection(conn, cid)
    began = time.monotonic()
    try:
        hits = await search.search(conn, body.collection_ids, body.query, body.limit)
    except EmbedError as e:
        raise HTTPException(503, e.message)
    except VectorError as e:
        raise HTTPException(503, str(e))
    return {"hits": hits, "duration_ms": int((time.monotonic() - began) * 1000)}


class NewChatsIn(BaseModel):
    collection_ids: list[str]


@router.get("/new-chats")
async def get_new_chats(conn=Depends(get_conn)):
    """The collections every chat reads that nobody chose collections for (repo/kb.py): new chats, and the ones that exist."""
    return {"collection_ids": await kb.new_chat_collections(conn)}


@router.put("/new-chats")
async def put_new_chats(body: NewChatsIn, conn=Depends(get_conn)):
    for cid in body.collection_ids:
        await _collection(conn, cid)
    return {"collection_ids": await kb.set_new_chat_collections(conn, body.collection_ids)}


@router.get("/chats/{chat_id}")
async def get_chat_kb(chat_id: str, conn=Depends(get_conn)):
    if not await chats.get(conn, chat_id):
        raise not_found("chat")
    return await kb.get_chat_kb(conn, chat_id)


@router.put("/chats/{chat_id}")
async def put_chat_kb(chat_id: str, body: ChatKbIn, conn=Depends(get_conn)):
    if not await chats.get(conn, chat_id):
        raise not_found("chat")
    for cid in body.collection_ids or []:
        await _collection(conn, cid)
    return await kb.set_chat_kb(conn, chat_id, collection_ids=body.collection_ids,
                                always_retrieve=body.always_retrieve)
