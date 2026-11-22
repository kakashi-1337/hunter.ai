"""Knowledge collections: documents, chunks, the keyword index, ingest job items, and which
collections a chat reads from. Chunk text is stored once; the FTS index points at it."""
from __future__ import annotations

import builtins
import sqlite3
import uuid
from typing import Literal

from ..db import new_id, now
from ..types import ChatKb, Chunk, ChunkDraft, Collection, Document, JobItem
from . import jobs, settings

CHUNK_NS = uuid.UUID("9d2f6c1e-5b7a-4c3d-8e21-48756e7465ca")
STATUSES = ("ok", "skipped", "failed", "processing")


def chunk_id(document_id: str, ordinal: int) -> str:
    return uuid.uuid5(CHUNK_NS, f"{document_id}:{ordinal}").hex


def vector_name(collection_id: str) -> str:
    """The only name ever given to the vector store for a collection."""
    return "kb_" + collection_id


def _coll(r) -> Collection:
    return Collection(id=r["id"], name=r["name"], embedder=r["embedder"], dimension=r["dimension"],
                      created_at=r["created_at"])


def _doc(r, prefix: str = "") -> Document:
    g = lambda k: r[prefix + k]  # noqa: E731
    return Document(id=g("id"), collection_id=g("collection_id"), source_kind=g("source_kind"),
                    source_uri=g("source_uri"), sha256=g("sha256"), size=g("size"), status=g("status"),
                    reason=g("reason"), chunk_count=g("chunk_count"), job_id=g("job_id"),
                    created_at=g("created_at"), updated_at=g("updated_at"))


def _chunk(r) -> Chunk:
    return Chunk(id=r["id"], document_id=r["document_id"], collection_id=r["collection_id"],
                 ordinal=r["ordinal"], text=r["text"], char_start=r["char_start"], char_end=r["char_end"],
                 line_start=r["line_start"], line_end=r["line_end"], embedded=bool(r["embedded"]))


# ---- collections

async def create_collection(conn, name: str, embedder: str, dimension: int) -> Collection:
    cid = new_id()
    try:
        await conn.execute("insert into kb_collections(id,name,embedder,dimension,created_at) "
                           "values(?,?,?,?,?)", (cid, name, embedder, dimension, now()))
    except sqlite3.IntegrityError:
        await conn.rollback()
        raise ValueError("name taken") from None
    await conn.commit()
    return await get_collection(conn, cid)


async def get_collection(conn, cid: str) -> Collection | None:
    r = await (await conn.execute("select * from kb_collections where id=?", (cid,))).fetchone()
    return _coll(r) if r else None


async def list_collections(conn) -> list[Collection]:
    rows = await (await conn.execute("select * from kb_collections order by name collate nocase")).fetchall()
    return [_coll(r) for r in rows]


async def rename_collection(conn, cid: str, name: str) -> Collection:
    try:
        await conn.execute("update kb_collections set name=? where id=?", (name, cid))
    except sqlite3.IntegrityError:
        await conn.rollback()
        raise ValueError("name taken") from None
    await conn.commit()
    return await get_collection(conn, cid)


async def delete_collection(conn, cid: str) -> None:
    # Chunk rows go first and explicitly, so the FTS triggers fire for every one of them.
    await conn.execute("delete from kb_chunks where collection_id=?", (cid,))
    await conn.execute("delete from kb_documents where collection_id=?", (cid,))
    await conn.execute("delete from chat_kb_collections where collection_id=?", (cid,))
    await conn.execute("delete from kb_collections where id=?", (cid,))
    await conn.commit()


# ---- documents

async def find_document(conn, collection_id: str, source_uri: str) -> Document | None:
    r = await (await conn.execute("select * from kb_documents where collection_id=? and source_uri=?",
                                  (collection_id, source_uri))).fetchone()
    return _doc(r) if r else None


async def get_document(conn, did: str) -> Document | None:
    r = await (await conn.execute("select * from kb_documents where id=?", (did,))).fetchone()
    return _doc(r) if r else None


async def _upsert(conn, collection_id: str, source_kind: str, source_uri: str, *, status: str,
                  reason: str | None, sha256: str, size: int, job_id: str | None) -> str:
    """Insert or reuse the row for (collection, uri), keeping its id; its old chunks go."""
    t = now()
    r = await (await conn.execute("select id from kb_documents where collection_id=? and source_uri=?",
                                  (collection_id, source_uri))).fetchone()
    if r:
        did = r["id"]
        await conn.execute("delete from kb_chunks where document_id=?", (did,))
        await conn.execute(
            "update kb_documents set source_kind=?, sha256=?, size=?, status=?, reason=?, chunk_count=0, "
            "job_id=?, updated_at=? where id=?", (source_kind, sha256, size, status, reason, job_id, t, did))
    else:
        did = new_id()
        await conn.execute(
            "insert into kb_documents(id,collection_id,source_kind,source_uri,sha256,size,status,reason,"
            "job_id,created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (did, collection_id, source_kind, source_uri, sha256, size, status, reason, job_id, t, t))
    return did


async def begin_document(conn, collection_id: str, source_kind: str, source_uri: str, sha256: str,
                         size: int, job_id: str | None, drafts: list[ChunkDraft],
                         note: str | None = None) -> Document:
    """One transaction that awaits nothing but the database: the row becomes `processing` and
    its chunks are replaced by `drafts`, all not yet embedded."""
    did = await _upsert(conn, collection_id, source_kind, source_uri, status="processing", reason=note,
                        sha256=sha256, size=size, job_id=job_id)
    await conn.executemany(
        "insert into kb_chunks(id,document_id,collection_id,ordinal,text,char_start,char_end,"
        "line_start,line_end,embedded) values(?,?,?,?,?,?,?,?,?,0)",
        [(chunk_id(did, d.ordinal), did, collection_id, d.ordinal, d.text, d.char_start, d.char_end,
          d.line_start, d.line_end) for d in drafts])
    await conn.commit()
    return await get_document(conn, did)


async def record_document(conn, collection_id: str, source_kind: str, source_uri: str, *,
                          status: Literal["skipped", "failed"], reason: str, sha256: str = "",
                          size: int = 0, job_id: str | None = None) -> Document:
    did = await _upsert(conn, collection_id, source_kind, source_uri, status=status, reason=reason,
                        sha256=sha256, size=size, job_id=job_id)
    await conn.commit()
    return await get_document(conn, did)


async def complete_document(conn, document_id: str) -> Document:
    await conn.execute(
        "update kb_documents set status='ok', updated_at=?, chunk_count="
        "(select count(*) from kb_chunks where document_id=?) where id=?", (now(), document_id, document_id))
    await conn.commit()
    return await get_document(conn, document_id)


async def delete_document(conn, document_id: str) -> None:
    await conn.execute("delete from kb_chunks where document_id=?", (document_id,))
    await conn.execute("delete from kb_documents where id=?", (document_id,))
    await conn.commit()


async def pending_chunks(conn, document_id: str, limit: int) -> list[Chunk]:
    rows = await (await conn.execute(
        "select * from kb_chunks where document_id=? and embedded=0 order by ordinal limit ?",
        (document_id, limit))).fetchall()
    return [_chunk(r) for r in rows]


async def mark_embedded(conn, chunk_ids: list[str]) -> None:
    await conn.executemany("update kb_chunks set embedded=1 where id=?", [(c,) for c in chunk_ids])
    await conn.commit()


async def embedded_count(conn, document_id: str) -> int:
    return (await (await conn.execute("select count(*) from kb_chunks where document_id=? and embedded=1",
                                      (document_id,))).fetchone())[0]


async def list_documents(conn, collection_id: str, status: str | None = None, limit: int = 100,
                         offset: int = 0) -> tuple[int, list[Document]]:
    where, args = "collection_id=?", [collection_id]
    if status:
        where += " and status=?"; args.append(status)
    total = (await (await conn.execute(f"select count(*) from kb_documents where {where}", args)).fetchone())[0]
    rows = await (await conn.execute(
        f"select * from kb_documents where {where} order by updated_at desc, rowid desc limit ? offset ?",
        (*args, limit, offset))).fetchall()
    return total, [_doc(r) for r in rows]


async def counts(conn, collection_id: str) -> dict:
    docs = dict.fromkeys(STATUSES, 0)
    for r in await (await conn.execute("select status, count(*) c from kb_documents where collection_id=? "
                                       "group by status", (collection_id,))).fetchall():
        docs[r["status"]] = r["c"]
    chunks = (await (await conn.execute("select count(*) from kb_chunks where collection_id=?",
                                        (collection_id,))).fetchone())[0]
    return {"documents": docs, "chunks": chunks}


# ---- ingest job items

async def add_items(conn, job_id: str, items: list[tuple[str, str, int]]) -> int:
    before = conn.total_changes
    await conn.executemany("insert or ignore into kb_job_items(job_id,uri,path,size) values(?,?,?,?)",
                           [(job_id, *i) for i in items])
    await conn.commit()
    return conn.total_changes - before


def _item(r) -> JobItem:
    return JobItem(n=r["n"], job_id=r["job_id"], uri=r["uri"], path=r["path"], size=r["size"], state=r["state"])


async def next_item(conn, job_id: str) -> JobItem | None:
    r = await (await conn.execute("select * from kb_job_items where job_id=? and state='pending' "
                                  "order by n limit 1", (job_id,))).fetchone()
    return _item(r) if r else None


async def reset_items(conn, job_id: str) -> None:
    await conn.execute("delete from kb_job_items where job_id=?", (job_id,))
    await conn.commit()


async def finish_item(conn, item: JobItem, outcome: Literal["ok", "unchanged", "skipped", "failed"]) -> None:
    """One transaction: the item is done and the job's progress and statistics move with it."""
    await conn.execute("update kb_job_items set state='done' where n=?", (item.n,))
    await jobs.progress(conn, item.job_id, done=1, add={outcome: 1, "bytes_done": item.size}, commit=False)
    await conn.commit()


# ---- search support

async def keyword_ids(conn, collection_ids: list[str], match: str, limit: int) -> list[str]:
    """Chunk ids by FTS5 bm25, best first; only chunks of `ok` documents in these collections."""
    if not collection_ids:
        return []
    marks = ",".join("?" * len(collection_ids))
    rows = await (await conn.execute(
        f"select c.id from kb_chunks_fts f join kb_chunks c on c.n = f.rowid "
        f"join kb_documents d on d.id = c.document_id where kb_chunks_fts match ? and d.status='ok' "
        f"and c.collection_id in ({marks}) order by bm25(kb_chunks_fts), c.n limit ?",
        (match, *collection_ids, limit))).fetchall()
    return [r["id"] for r in rows]


async def exact_ids(conn, collection_ids: list[str], phrase: str, spelling: str, limit: int, *, any_case: bool = False) -> list[str]:
    """Chunk ids, oldest first, whose text holds `spelling` letter for letter (with `any_case`, in whatever case).
    The keyword index only knows words: to it `wait_for` is "wait for". So it is asked for the passages that have the
    words of `phrase` side by side, and the text of every one of those is looked at. Nothing is cut off before that
    look: an ordinary phrase can stand in hundreds of passages and the identifier in one of them. Only chunks of `ok`
    documents in these collections."""
    if not collection_ids:
        return []
    marks = ",".join("?" * len(collection_ids))
    holds = "instr(lower(c.text), lower(?)) > 0" if any_case else "instr(c.text, ?) > 0"
    rows = await (await conn.execute(
        f"select c.id from kb_chunks_fts f join kb_chunks c on c.n = f.rowid "
        f"join kb_documents d on d.id = c.document_id where kb_chunks_fts match ? and d.status='ok' "
        f"and c.collection_id in ({marks}) and {holds} order by c.n limit ?",
        (phrase, *collection_ids, spelling, limit))).fetchall()
    return [r["id"] for r in rows]


async def texts_until(conn, document_id: str, ordinal: int) -> list[str]:
    """The passages of a document that stand before its passage `ordinal`, in their order."""
    rows = await (await conn.execute("select text from kb_chunks where document_id=? and ordinal<? order by ordinal",
                                     (document_id, ordinal))).fetchall()
    return [r["text"] for r in rows]


async def chunks_by_id(conn, ids: list[str]) -> dict[str, tuple[Chunk, Document]]:
    out: dict[str, tuple[Chunk, Document]] = {}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        rows = await (await conn.execute(
            f"select c.*, d.id d_id, d.collection_id d_collection_id, d.source_kind d_source_kind, "
            f"d.source_uri d_source_uri, d.sha256 d_sha256, d.size d_size, d.status d_status, "
            f"d.reason d_reason, d.chunk_count d_chunk_count, d.job_id d_job_id, d.created_at d_created_at, "
            f"d.updated_at d_updated_at from kb_chunks c join kb_documents d on d.id = c.document_id "
            f"where d.status='ok' and c.id in ({','.join('?' * len(part))})", part)).fetchall()
        for r in rows:
            out[r["id"]] = (_chunk(r), _doc(r, "d_"))
    return out


# ---- which collections a chat reads

async def get_chat_kb(conn, chat_id: str) -> ChatKb:
    r = await (await conn.execute("select always_retrieve from chat_kb where chat_id=?", (chat_id,))).fetchone()
    rows = await (await conn.execute(
        "select l.collection_id from chat_kb_collections l join kb_collections c on c.id = l.collection_id "
        "where l.chat_id=? order by c.name collate nocase", (chat_id,))).fetchall()
    return ChatKb(chat_id=chat_id, collection_ids=[x["collection_id"] for x in rows],
                  always_retrieve=bool(r["always_retrieve"]) if r else False)


async def set_chat_kb(conn, chat_id: str, *, collection_ids: builtins.list[str] | None = None,
                      always_retrieve: bool | None = None) -> ChatKb:
    await conn.execute("insert or ignore into chat_kb(chat_id) values(?)", (chat_id,))
    if always_retrieve is not None:
        await conn.execute("update chat_kb set always_retrieve=? where chat_id=?", (int(always_retrieve), chat_id))
    if collection_ids is not None:
        await conn.execute("delete from chat_kb_collections where chat_id=?", (chat_id,))
        await conn.executemany("insert or ignore into chat_kb_collections(chat_id,collection_id) values(?,?)",
                               [(chat_id, c) for c in dict.fromkeys(collection_ids)])
    await conn.commit()
    return await get_chat_kb(conn, chat_id)


# ---- which collections every new chat starts with

NEW_CHATS = "kb_new_chats"      # the setting: the ids the owner named


async def new_chat_collections(conn) -> builtins.list[str]:
    """The collections every new chat starts with: those the owner named that still exist, by name as a chat
    lists its own. None unless he named some: a chat without a collection is the usual one."""
    named = await settings.get(conn, NEW_CHATS, [])
    if not isinstance(named, builtins.list) or not named:
        return []
    marks = ",".join("?" * len(named))
    rows = await (await conn.execute(
        f"select id from kb_collections where id in ({marks}) order by name collate nocase", [str(c) for c in named])).fetchall()
    return [r["id"] for r in rows]


async def set_new_chat_collections(conn, collection_ids: builtins.list[str]) -> builtins.list[str]:
    await settings.put(conn, NEW_CHATS, builtins.list(collection_ids))      # read back each once: see above
    return await new_chat_collections(conn)


# ---- index browser: one chunk at a time

async def spread_chunks(conn, collection_id: str, limit: int) -> list[tuple[str, str, str]]:
    """(chunk id, text, where it came from) of `limit` chunks at most, spread evenly over the collection: every k-th
    in the order they were indexed, so that every part of a large collection is among them. A collection that fits
    is given whole. Always the same chunks for the same collection. Only chunks of `ok` documents."""
    total = (await (await conn.execute("select count(*) from kb_chunks where collection_id=?", (collection_id,))).fetchone())[0]
    step = max(1, -(-total // max(limit, 1)))               # rounded up: never more than `limit` of them
    rows = await (await conn.execute(
        "select c.id, c.text, d.source_uri from kb_chunks c join kb_documents d on d.id = c.document_id "
        "where c.collection_id=? and d.status='ok' and c.n % ? = 0 order by c.n limit ?",
        (collection_id, step, limit))).fetchall()
    return [(r["id"], r["text"], r["source_uri"]) for r in rows]


async def indexed_documents(conn, collection_id: str) -> list[Document]:
    """Every document of a collection that is searched (`ok`), in the order of where they came from."""
    rows = await (await conn.execute("select * from kb_documents where collection_id=? and status='ok' order by source_uri",
                                     (collection_id,))).fetchall()
    return [_doc(r) for r in rows]


async def chunk_texts(conn, document_id: str) -> list[str]:
    """The passages of a document, in its order."""
    rows = await (await conn.execute("select text from kb_chunks where document_id=? order by ordinal", (document_id,))).fetchall()
    return [r["text"] for r in rows]


async def list_chunks(conn, document_id: str, limit: int = 100, offset: int = 0) -> tuple[int, list[Chunk]]:
    total = (await (await conn.execute("select count(*) from kb_chunks where document_id=?", (document_id,))).fetchone())[0]
    rows = await (await conn.execute("select * from kb_chunks where document_id=? order by ordinal limit ? offset ?",
                                     (document_id, limit, offset))).fetchall()
    return total, [_chunk(r) for r in rows]


async def get_chunk(conn, chunk_id: str) -> Chunk | None:
    r = await (await conn.execute("select * from kb_chunks where id=?", (chunk_id,))).fetchone()
    return _chunk(r) if r else None


async def update_chunk_text(conn, chunk_id: str, text: str) -> Chunk:
    """The keyword index follows through the update trigger. Offsets keep describing the
    place in the original file the chunk came from."""
    await conn.execute("update kb_chunks set text=?, embedded=1 where id=?", (text, chunk_id))
    await conn.execute("update kb_documents set updated_at=? where id=(select document_id from kb_chunks where id=?)",
                       (now(), chunk_id))
    await conn.commit()
    return await get_chunk(conn, chunk_id)


async def delete_chunk(conn, chunk_id: str) -> None:
    r = await (await conn.execute("select document_id from kb_chunks where id=?", (chunk_id,))).fetchone()
    if not r:
        return
    await conn.execute("delete from kb_chunks where id=?", (chunk_id,))
    await conn.execute("update kb_documents set updated_at=?, chunk_count=(select count(*) from kb_chunks "
                       "where document_id=?) where id=?", (now(), r["document_id"], r["document_id"]))
    await conn.commit()


async def pending_in_collection(conn, collection_id: str, limit: int) -> list[Chunk]:
    rows = await (await conn.execute("select * from kb_chunks where collection_id=? and embedded=0 order by n limit ?",
                                     (collection_id, limit))).fetchall()
    return [_chunk(r) for r in rows]


async def start_reembed(conn, collection_id: str, embedder: str, dimension: int) -> int:
    """The collection takes the new embedder and every chunk waits to be embedded again."""
    await conn.execute("update kb_collections set embedder=?, dimension=? where id=?", (embedder, dimension, collection_id))
    await conn.execute("update kb_chunks set embedded=0 where collection_id=?", (collection_id,))
    await conn.commit()
    return (await (await conn.execute("select count(*) from kb_chunks where collection_id=?", (collection_id,))).fetchone())[0]
