"""One ingest job: discover the files, then per file fetch, clean, chunk, embed, upsert.
Every step is checkpointed. What to redo after a crash is read from the stored state (item
rows, the document's status and hash, each chunk's embedded flag), never from memory."""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ..repo import jobs, kb, settings
from ..types import Collection, Document, Job
from .. import thermal
from . import chunking, embed, generated, limits, parsers, screen, sources
from .archives import Found
from .embed import EmbedError
from .vectors import Point, VectorError, VectorStore, get_store

RETRY_DELAYS = (2, 8, 30)
DISCOVER_BATCH = 500
DISCOVER_CHECK_EVERY = 200      # discovered entries between two looks at pause, cancel, and stop
KIND = "ingest"
STATS = ("discovered", "ok", "unchanged", "skipped", "failed", "chunks", "bytes_total", "bytes_done")
_sleep = asyncio.sleep


class _Leave(Exception):
    """This call of run_job ends now, in the given job state."""

    def __init__(self, state: str):
        self.state = state


def _source_kind(payload: dict) -> str:
    return payload["source"]["kind"]


async def set_aside(conn, store: VectorStore, col: Collection, source_kind: str, uri: str,
                    status: Literal["skipped", "failed"], reason: str, *, job_id: str | None,
                    sha256: str = "", size: int = 0) -> Document:
    """The one path for every skipped or failed document, in a job and when a collection is cleaned
    (kb/clean.py). Its vectors go first, so a crash between the two steps cannot leave vectors that
    no later run would find."""
    doc = await kb.find_document(conn, col.id, uri)
    if doc is not None and doc.status in ("ok", "processing"):
        await store.delete_by_document(kb.vector_name(col.id), doc.id)
    return await kb.record_document(conn, col.id, source_kind, uri, status=status, reason=reason,
                                    sha256=sha256, size=size, job_id=job_id)


async def cleanup(conn, *, store: VectorStore | None = None) -> int:
    """Janitor for finished and cancelled ingest jobs; safe to call again and again."""
    store = store or get_store()
    cleaned = 0
    for job in await jobs.list(conn, kind=KIND, states=["done", "cancelled"], limit=None):
        if job.checkpoint.get("phase") == "cleaned":
            continue
        try:
            if job.state == "cancelled":
                rows = await (await conn.execute(
                    "select id, collection_id from kb_documents where job_id=? and status='processing'",
                    (job.id,))).fetchall()
                for r in rows:
                    try:
                        await store.delete_by_document(kb.vector_name(r["collection_id"]), r["id"])
                    except VectorError as e:
                        if "unknown vector collection" not in str(e):
                            raise
                    await conn.execute("delete from kb_chunks where document_id=?", (r["id"],))
                    await conn.execute("update kb_documents set status='failed', reason='ingest cancelled', "
                                       "chunk_count=0 where id=?", (r["id"],))
                    await conn.commit()
        except VectorError:
            continue                      # the store is down: this job waits for the next call
        await kb.reset_items(conn, job.id)
        sid = job.payload.get("staging_id")
        if sid:
            try:
                sources.remove_staging(sid)
            except ValueError:
                pass
        await jobs.checkpoint(conn, job.id, {"phase": "cleaned"})
        cleaned += 1
    return cleaned


async def _unload(used: list) -> None:
    """The end of a job, however it ends: the model it kept loaded between its batches is unloaded.
    Left behind, it would sit in memory beside whatever the machine does next."""
    for embedder in used:
        await asyncio.shield(embedder.release())


async def run_job(conn, job: Job, *, store: VectorStore | None = None,
                  stop: Callable[[], bool] = lambda: False) -> str:
    """Run one claimed ingest job. Returns the state it left the job in."""
    used: list = []
    try:
        return await _run_job(conn, job, used, store=store, stop=stop)
    finally:
        await _unload(used)


async def _run_job(conn, job: Job, used: list, *, store: VectorStore | None = None,
                   stop: Callable[[], bool] = lambda: False) -> str:
    payload = job.payload
    col = await kb.get_collection(conn, payload.get("collection_id", ""))
    if col is None:
        await jobs.finish(conn, job.id, "failed", "collection no longer exists")
        return "failed"
    store = store or get_store()
    name = kb.vector_name(col.id)
    kind = _source_kind(payload)
    # What nobody wrote (build output, bundles, source maps, lock files) is left out, unless the owner switched
    # that off. A page of a crawled site was asked for: it is taken as it is.
    screened = kind != "web" and bool(await settings.get(conn, "kb_skip_generated", True))
    began = time.monotonic()
    mine = {"chunks": 0, "bytes": 0, "items": 0}

    async def check() -> None:
        if stop():
            await jobs.release(conn, job.id)
            raise _Leave("queued")
        state = await jobs.state(conn, job.id)
        if state != "running":
            if state == "cancelled":
                await cleanup(conn, store=store)
            raise _Leave(state or "cancelled")
        try:
            limits.require_space()
        except limits.NoSpace as e:
            await jobs.finish(conn, job.id, "failed", str(e))
            raise _Leave("failed") from None

    async def patiently(what: str, call):
        """Retry an embedder or vector-store outage; a lasting one fails the job, resumably."""
        for delay in (*RETRY_DELAYS, None):
            try:
                return await call()
            except EmbedError as e:
                if e.kind != "unavailable":
                    raise
                error = f"embedder unavailable: {e.message}"
            except VectorError as e:
                error = f"vector store unavailable: {e}"
            if delay is None:
                await jobs.finish(conn, job.id, "failed", error)
                raise _Leave("failed")
            await _sleep(delay)

    try:
        embedder = embed.get(col.embedder, stays_loaded=True)
    except ValueError as e:
        await jobs.finish(conn, job.id, "failed", str(e))
        return "failed"
    used.append(embedder)

    try:
        # ---- discover
        if job.checkpoint.get("phase") in (None, "discover"):
            await jobs.checkpoint(conn, job.id, {"phase": "discover"})
            await kb.reset_items(conn, job.id)
            shutil.rmtree(sources.staging_path(payload["staging_id"]) / "unpacked", ignore_errors=True)
            await conn.execute("update jobs set stats_json='{}', progress_done=0, progress_total=0 where id=?",
                               (job.id,))
            await jobs.progress(conn, job.id, set={**dict.fromkeys(STATS, 0), "embed_rate": 0.0,
                                                   "eta_seconds": None})
            batch: list[tuple[str, str, int]] = []
            total = size = 0

            async def flush():
                nonlocal batch
                if batch:
                    await kb.add_items(conn, job.id, batch)
                    await jobs.progress(conn, job.id, add={"discovered": len(batch)})
                    batch = []
            seen, whole = 0, None
            # A clone or a crawl can take minutes before the first file is known: say how much has arrived meanwhile.
            watch = asyncio.create_task(watch_fetch(conn, job.id, sources.staging_path(payload["staging_id"]))) \
                if kind in ("git", "web") else None
            async for x in _then(sources.discover(payload, skip_generated=screened), watch):
                seen += 1
                if seen % (1 if kind in ("web", "git") else DISCOVER_CHECK_EVERY) == 0:   # a crawl is slow
                    await check()       # a discovery that is left is redone from the start
                if isinstance(x, Found):
                    batch.append((x.uri, str(x.path), x.size))
                    total += 1
                    size += x.size
                    if len(batch) >= DISCOVER_BATCH:
                        await flush()
                else:
                    await patiently("vectors", lambda x=x: set_aside(
                        conn, store, col, kind, x.uri, x.status, x.reason, job_id=job.id))
                    await jobs.progress(conn, job.id, add={x.status: 1})
                    if x.status == "failed" and x.uri == _where(payload):
                        whole = x.reason                    # the source itself could not be had, not one file of it
            await flush()
            if not total and whole is not None:
                # Nothing came in at all: that is a failed job with its reason and a Retry, not a "done" one with no files.
                await jobs.finish(conn, job.id, "failed", whole[:300])
                raise _Leave("failed")
            await jobs.progress(conn, job.id, total=total, set={"bytes_total": size})
            await jobs.checkpoint(conn, job.id, {"phase": "process"})

        # ---- process
        while True:
            await check()
            item = await kb.next_item(conn, job.id)
            if item is None:
                await jobs.finish(conn, job.id, "done")
                await cleanup(conn, store=store)
                return "done"

            async def stage(stage_name: str, item=item) -> None:
                await jobs.checkpoint(conn, job.id, {"phase": "process", "item": item.n, "uri": item.uri,
                                                     "stage": stage_name})

            async def aside(status, reason, **kw) -> str:
                await patiently("vectors", lambda: set_aside(conn, store, col, kind, item.uri, status,
                                                             reason, job_id=job.id, **kw))
                return status

            async def one() -> str:
                await stage("fetch")
                try:
                    data = await asyncio.to_thread(Path(item.path).read_bytes)
                except OSError as e:
                    return await aside("failed", f"unreadable: {type(e).__name__}")
                sha = hashlib.sha256(data).hexdigest()
                file_name = Path(item.path).name
                if screened and generated.has_shape(file_name):
                    # Asked before "unchanged": a minified file that was indexed before this was asked goes out now.
                    minified = await asyncio.to_thread(generated.by_shape, file_name, [data])
                    if minified:
                        return await aside("skipped", minified, sha256=sha, size=len(data))
                doc = await kb.find_document(conn, col.id, item.uri)
                resume = False
                if doc is not None and doc.sha256 == sha:
                    if doc.status == "ok":
                        return "unchanged"
                    resume = doc.status == "processing" and bool(
                        await kb.embedded_count(conn, doc.id) or await kb.pending_chunks(conn, doc.id, 1))
                if not resume:
                    await stage("clean")
                    try:
                        parsed = await asyncio.to_thread(parsers.parse, file_name, data)
                    except parsers.Skip as e:
                        return await aside("skipped", e.reason, sha256=sha, size=len(data))
                    if await settings.get(conn, "kb_redact", True):
                        parsed.text, masked = await asyncio.to_thread(screen.redact, parsed.text)
                        if masked:
                            n = sum(masked.values())
                            said = f"{n} secret{'' if n == 1 else 's'} masked before indexing ({', '.join(sorted(masked))})"
                            parsed.note = f"{parsed.note}; {said}" if parsed.note else said
                    await stage("chunk")
                    drafts = await asyncio.to_thread(chunking.chunk, parsed.text, parsed.kind)
                    if not drafts:
                        return await aside("skipped", "no text content", sha256=sha, size=len(data))
                    if doc is not None:
                        await patiently("vectors", lambda: store.delete_by_document(name, doc.id))
                    doc = await kb.begin_document(conn, col.id, kind, item.uri, sha, len(data), job.id,
                                                  drafts, note=parsed.note)
                elif await kb.embedded_count(conn, doc.id) == 0:
                    await patiently("vectors", lambda: store.delete_by_document(name, doc.id))
                while True:
                    await check()
                    chunks = await kb.pending_chunks(conn, doc.id, embed.EMBED_BATCH)
                    if not chunks:
                        break
                    await stage("embed")
                    await cool_down(conn, job.id, check)
                    try:
                        vectors = await patiently("embed", lambda: embedder.embed([c.text for c in chunks]))
                    except EmbedError as e:          # bad_request: this document only
                        return await aside("failed", e.message, sha256=sha, size=len(data))
                    if any(len(v) != col.dimension for v in vectors):
                        await jobs.finish(conn, job.id, "failed", f"embedder returned {len(vectors[0])} "
                                          f"dimensions, collection expects {col.dimension}")
                        raise _Leave("failed")
                    await stage("upsert")
                    points = [Point(c.id, doc.id, v) for c, v in zip(chunks, vectors)]
                    await patiently("vectors", lambda: store.upsert(name, points))
                    await kb.mark_embedded(conn, [c.id for c in chunks])
                    mine["chunks"] += len(chunks)
                    seconds = max(time.monotonic() - began, 1e-6)
                    left = await _bytes_left(conn, job.id)
                    eta = round(left / (mine["bytes"] / seconds)) if mine["items"] and mine["bytes"] else None
                    await jobs.progress(conn, job.id, add={"chunks": len(chunks)},
                                        set={"embed_rate": round(mine["chunks"] / seconds, 1), "eta_seconds": eta})
                await kb.complete_document(conn, doc.id)
                return "ok"

            try:
                outcome = await one()
            except _Leave:
                raise
            except Exception as e:      # noqa: BLE001 - one bad file must not stop the job
                outcome = await aside("failed", f"{type(e).__name__}: {e}"[:300])
            await kb.finish_item(conn, item, outcome)
            mine["items"] += 1
            mine["bytes"] += item.size
    except _Leave as leave:
        return leave.state


async def _bytes_left(conn, job_id: str) -> int:
    j = await jobs.get(conn, job_id)
    return max(0, j.stats.get("bytes_total", 0) - j.stats.get("bytes_done", 0)) if j else 0


async def run_reembed(conn, job: Job, *, store: VectorStore | None = None,
                      stop: Callable[[], bool] = lambda: False) -> str:
    """Embed a whole collection again with another embedder. The vectors are dropped first
    (their size may differ), so search finds less until the job is done."""
    used: list = []
    try:
        return await _run_reembed(conn, job, used, store=store, stop=stop)
    finally:
        await _unload(used)


async def _run_reembed(conn, job: Job, used: list, *, store: VectorStore | None = None,
                       stop: Callable[[], bool] = lambda: False) -> str:
    col = await kb.get_collection(conn, job.payload.get("collection_id", ""))
    if col is None:
        await jobs.finish(conn, job.id, "failed", "collection no longer exists")
        return "failed"
    store = store or get_store()
    name = kb.vector_name(col.id)
    spec = job.payload["embedder"]
    began, done_here = time.monotonic(), 0

    async def fail(error: str) -> str:
        await jobs.finish(conn, job.id, "failed", error)
        return "failed"

    async def attempt(call):
        """(result, None) or (None, error text) after the usual retries."""
        for delay in (*RETRY_DELAYS, None):
            try:
                return await call(), None
            except EmbedError as e:
                if e.kind != "unavailable":
                    return None, e.message
                error = f"embedder unavailable: {e.message}"
            except VectorError as e:
                error = f"vector store unavailable: {e}"
            if delay is None:
                return None, error
            await _sleep(delay)
    try:
        embedder = embed.get(spec, stays_loaded=True)
    except ValueError as e:
        return await fail(str(e))
    used.append(embedder)
    if job.checkpoint.get("phase") != "embed":
        dim, error = await attempt(lambda: embed.dimension(embedder))
        if error:
            return await fail(error)
        # The last moment to change one's mind: after this the old vectors are gone.
        if stop():
            await jobs.release(conn, job.id)
            return "queued"
        state = await jobs.state(conn, job.id)
        if state != "running":
            return state or "cancelled"

        async def reset():
            await store.drop(name)
            await store.ensure_collection(name, dim)
        _, error = await attempt(reset)
        if error:
            return await fail(error)
        total = await kb.start_reembed(conn, col.id, spec, dim)
        await conn.execute("update jobs set stats_json='{}', progress_done=0 where id=?", (job.id,))
        await jobs.progress(conn, job.id, total=total, set={"chunks": 0, "embed_rate": 0.0, "eta_seconds": None})
        await jobs.checkpoint(conn, job.id, {"phase": "embed"})
        col = await kb.get_collection(conn, col.id)
    while True:
        if stop():
            await jobs.release(conn, job.id)
            return "queued"
        state = await jobs.state(conn, job.id)
        if state != "running":
            return state or "cancelled"
        chunks = await kb.pending_in_collection(conn, col.id, embed.EMBED_BATCH)
        if not chunks:
            await jobs.finish(conn, job.id, "done")
            return "done"
        await cool_down(conn, job.id)
        vectors, error = await attempt(lambda: embedder.embed([c.text for c in chunks]))
        if error and error.startswith(("embedder unavailable", "vector store unavailable")):
            return await fail(error)
        if error:
            # The new model refuses something in this batch: find it one chunk at a time, drop
            # only that chunk from the index, and go on. One bad chunk must not block the rest.
            kept, vectors = [], []
            for c in chunks:
                one, why = await attempt(lambda c=c: embedder.embed([c.text]))
                if why and why.startswith(("embedder unavailable", "vector store unavailable")):
                    return await fail(why)
                if why:
                    await kb.delete_chunk(conn, c.id)
                    await jobs.progress(conn, job.id, done=1, add={"failed": 1}, set={"last_error": why[:200]})
                else:
                    kept.append(c)
                    vectors.append(one[0])
            chunks = kept
            if not chunks:
                continue
        if any(len(v) != col.dimension for v in vectors):
            return await fail(f"embedder returned {len(vectors[0])} dimensions, collection expects {col.dimension}")
        _, error = await attempt(lambda: store.upsert(name, [Point(c.id, c.document_id, v) for c, v in zip(chunks, vectors)]))
        if error:
            return await fail(error)
        await kb.mark_embedded(conn, [c.id for c in chunks])
        done_here += len(chunks)
        seconds = max(time.monotonic() - began, 1e-6)
        j = await jobs.get(conn, job.id)
        remaining = max(0, j.progress_total - j.progress_done - len(chunks))
        rate = done_here / seconds
        await jobs.progress(conn, job.id, done=len(chunks), add={"chunks": len(chunks)},
                            set={"embed_rate": round(rate, 1), "eta_seconds": round(remaining / rate)})


FETCH_WATCH_EVERY = 3.0      # seconds between two looks at how much a clone or a crawl has brought in


def _tree_bytes(root: Path) -> int:
    total = 0
    for folder, _, files in os.walk(root, onerror=lambda e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


async def watch_fetch(conn, job_id: str, root: Path, every: float | None = None) -> None:
    """Until cancelled: write the size of what has been fetched into the job's stats."""
    last = -1
    while True:
        size = await asyncio.to_thread(_tree_bytes, root)
        if size != last:
            await jobs.progress(conn, job_id, set={"fetched_bytes": size})
            last = size
        await asyncio.sleep(FETCH_WATCH_EVERY if every is None else every)


async def _then(items, task):
    """`items`, with `task` cancelled once they are through or given up on."""
    try:
        async for x in items:
            yield x
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - the watcher's end is not the job's business
                pass


COOL_MARGIN = 5.0            # degrees under the limit at which a job that waited goes on
COOL_STEP = 3.0              # seconds between two looks at the temperature
COOLING = {thermal.PROCESSOR: "cooling", thermal.GRAPHICS: "cooling_gpu"}      # where a job says how hot the part is it waits for


async def cool_down(conn, job_id: str, check=None, step: float | None = None) -> None:
    """Before a batch is embedded: when the processor or the graphics card is at its limit
    (HUNTERAI_MAX_CPU_TEMP, HUNTERAI_MAX_GPU_TEMP), wait until it is a few degrees under it. Hours
    of embedding otherwise hold a laptop at the temperature where it switches itself off. The job
    says which part it waits for and how hot that is; `check` lets a pause or a cancel through
    while it waits."""
    shown = None

    async def tell(part: thermal.Part) -> None:
        nonlocal shown
        if shown is None or shown[0] != part.name or abs(part.degrees - shown[1]) >= 1:
            await jobs.progress(conn, job_id, set={**dict.fromkeys(COOLING.values()), COOLING[part.name]: part.degrees})
            shown = (part.name, part.degrees)
        if check is not None:
            await check()

    await thermal.wait_cool(math.inf, COOL_STEP if step is None else step, COOL_MARGIN, tell)
    if shown is not None:
        await jobs.progress(conn, job_id, set=dict.fromkeys(COOLING.values()))


def _where(payload: dict) -> str:
    source = payload.get("source") or {}
    return str(source.get("url") or source.get("path") or "")
