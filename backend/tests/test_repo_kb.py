import asyncio

import pytest

from hunterai import db
from hunterai.repo import chats, jobs, kb, settings
from hunterai.types import ChunkDraft


def drafts(*texts):
    return [ChunkDraft(ordinal=i, text=t, char_start=0, char_end=len(t), line_start=1, line_end=1) for i, t in enumerate(texts)]


async def coll(conn, name="notes"):
    return await kb.create_collection(conn, name, "fake:8", 8)


def test_chunk_ids_are_deterministic():
    assert kb.chunk_id("d", 3) == kb.chunk_id("d", 3) != kb.chunk_id("d", 4) and len(kb.chunk_id("d", 3)) == 32
    assert kb.vector_name("abc") == "kb_abc"


async def test_begin_document_replaces_chunks_and_keeps_id(conn):
    c = await coll(conn)
    d = await kb.begin_document(conn, c.id, "folder", "/a.txt", "a", 3, None, drafts("one", "two", "three"))
    await kb.mark_embedded(conn, [kb.chunk_id(d.id, 0)])
    d2 = await kb.begin_document(conn, c.id, "folder", "/a.txt", "b", 2, None, drafts("x", "y"), note="n")
    assert d2.id == d.id and (d2.status, d2.sha256, d2.reason) == ("processing", "b", "n")
    assert [ch.id for ch in await kb.pending_chunks(conn, d.id, 10)] == [kb.chunk_id(d.id, 0), kb.chunk_id(d.id, 1)]
    assert await kb.embedded_count(conn, d.id) == 0


async def test_keyword_ids_sees_only_ok_documents_and_follows_deletes(conn):
    c = await coll(conn); other = await coll(conn, "other")
    d = await kb.begin_document(conn, c.id, "folder", "/a.txt", "a", 3, None, drafts("union based sqli payloads"))
    assert await kb.keyword_ids(conn, [c.id], '"sqli"', 10) == []
    await kb.complete_document(conn, d.id)
    assert await kb.keyword_ids(conn, [c.id], '"sqli"', 10) == [kb.chunk_id(d.id, 0)]
    assert await kb.keyword_ids(conn, [other.id], '"sqli"', 10) == []
    await kb.record_document(conn, c.id, "folder", "/a.txt", status="failed", reason="gone")
    assert await kb.keyword_ids(conn, [c.id], '"sqli"', 10) == []
    assert (await (await conn.execute("select count(*) from kb_chunks")).fetchone())[0] == 0


async def test_complete_document_counts_chunks(conn):
    c = await coll(conn)
    d = await kb.begin_document(conn, c.id, "upload", "upload:a.md", "a", 3, None, drafts("one", "two"), note="n")
    done = await kb.complete_document(conn, d.id)
    assert (done.status, done.chunk_count, done.reason) == ("ok", 2, "n")
    got = await kb.chunks_by_id(conn, [kb.chunk_id(d.id, 0), kb.chunk_id(d.id, 1), "missing"])
    assert set(got) == {kb.chunk_id(d.id, 0), kb.chunk_id(d.id, 1)}
    chunk, doc = got[kb.chunk_id(d.id, 1)]
    assert chunk.text == "two" and doc.id == d.id


async def test_begin_document_with_many_chunks_leaves_room_for_another_writer(conn):
    c = await coll(conn); c2 = await db.connect()
    try:
        d, _ = await asyncio.gather(
            kb.begin_document(conn, c.id, "folder", "/big.txt", "a", 1, None, drafts(*[f"chunk {i}" for i in range(20_000)])),
            settings.put(c2, "k", "v"))
    finally:
        await c2.close()
    assert (await (await conn.execute("select count(*) from kb_chunks where document_id=? and embedded=0", (d.id,))).fetchone())[0] == 20_000


async def test_record_document_upserts_one_row_per_uri(conn):
    c = await coll(conn)
    await kb.record_document(conn, c.id, "folder", "/x.bin", status="skipped", reason="binary")
    d = await kb.record_document(conn, c.id, "folder", "/x.bin", status="skipped", reason="too large")
    total, docs = await kb.list_documents(conn, c.id)
    assert total == 1 and docs[0].id == d.id and docs[0].reason == "too large" and docs[0].chunk_count == 0


async def test_collection_name_unique_case_insensitive(conn):
    a = await coll(conn, "Notes"); b = await coll(conn, "other")
    with pytest.raises(ValueError, match="name taken"):
        await coll(conn, "notes")
    with pytest.raises(ValueError, match="name taken"):
        await kb.rename_collection(conn, b.id, "NOTES")
    assert (await kb.rename_collection(conn, a.id, "Field notes")).name == "Field notes"
    assert [c.name for c in await kb.list_collections(conn)] == ["Field notes", "other"]


async def test_delete_collection_removes_chunks_fts_and_chat_links(conn):
    c = await coll(conn)
    d = await kb.begin_document(conn, c.id, "folder", "/a.txt", "a", 3, None, drafts("sqli notes")); await kb.complete_document(conn, d.id)
    chat = await chats.create(conn, None, "pentesting", "mock", "m", {})
    await kb.set_chat_kb(conn, chat.id, collection_ids=[c.id])
    await kb.delete_collection(conn, c.id)
    for table in ("kb_documents", "kb_chunks", "chat_kb_collections"):
        assert (await (await conn.execute(f"select count(*) from {table}")).fetchone())[0] == 0
    assert await kb.keyword_ids(conn, [c.id], '"sqli"', 10) == [] and await kb.get_collection(conn, c.id) is None


async def test_items_and_finish_item(conn):
    j = await jobs.enqueue(conn, "ingest", {})
    assert await kb.add_items(conn, j.id, [("u1", "/p1", 5), ("u2", "/p2", 7)]) == 2
    assert await kb.add_items(conn, j.id, [("u2", "/p2", 7), ("u3", "/p3", 1)]) == 1
    item = await kb.next_item(conn, j.id)
    assert (item.uri, item.size) == ("u1", 5)
    await kb.finish_item(conn, item, "skipped")
    assert (await kb.next_item(conn, j.id)).uri == "u2"
    g = await jobs.get(conn, j.id)
    assert g.progress_done == 1 and g.stats == {"skipped": 1, "bytes_done": 5}
    await kb.reset_items(conn, j.id)
    assert await kb.next_item(conn, j.id) is None
    assert (await (await conn.execute("select count(*) from kb_job_items")).fetchone())[0] == 0


async def test_counts_shape(conn):
    c = await coll(conn)
    d = await kb.begin_document(conn, c.id, "folder", "/a", "a", 1, None, drafts("a", "b")); await kb.complete_document(conn, d.id)
    await kb.record_document(conn, c.id, "folder", "/b", status="skipped", reason="binary")
    await kb.begin_document(conn, c.id, "folder", "/c", "c", 1, None, drafts("c"))
    assert await kb.counts(conn, c.id) == {"documents": {"ok": 1, "skipped": 1, "failed": 0, "processing": 1}, "chunks": 3}
    total, docs = await kb.list_documents(conn, c.id, status="skipped")
    assert total == 1 and docs[0].source_uri == "/b"


async def test_chat_kb_defaults_set_and_cascade(conn):
    c = await coll(conn); chat = await chats.create(conn, None, "pentesting", "mock", "m", {})
    got = await kb.get_chat_kb(conn, chat.id)
    assert (got.collection_ids, got.always_retrieve, got.follows) == ([], True, True)      # what holds for every chat
    await kb.set_chat_kb(conn, chat.id, collection_ids=[c.id])
    got = await kb.set_chat_kb(conn, chat.id, always_retrieve=False)
    assert (got.collection_ids, got.always_retrieve, got.follows) == ([c.id], False, False)
    got = await kb.set_chat_kb(conn, chat.id, always_retrieve=True)
    assert (got.collection_ids, got.always_retrieve) == ([c.id], True)
    await chats.delete(conn, chat.id)
    for table in ("chat_kb", "chat_kb_collections"):
        assert (await (await conn.execute(f"select count(*) from {table}")).fetchone())[0] == 0
