import json
import tarfile
from pathlib import Path

import httpx
import respx

from hunterai.kb import embed, limits, pipeline, sources
from hunterai.kb.embed import EmbedError
from hunterai.kb.vectors import get_store
from hunterai.repo import jobs, kb
from tests.kb_samples import HANG, Crash, ingest_folder, make_pdf, tar_bytes, tree, zip_bytes

BIG = "\n\n".join(f"para {i} " + "word " * 180 for i in range(30)).encode()      # 30 chunks


async def rerun(conn, job):
    job = await jobs.claim(conn, ["ingest"])
    return await pipeline.run_job(conn, job), await jobs.get(conn, job.id)


async def test_folder_ingest_end_to_end(conn, col, tmp_path):
    root = tmp_path / "src"; store, name = get_store(), kb.vector_name(col.id)
    tree(root, {"a.md": ("# One\n\n" + "alpha " * 150 + "\n\n# Two\n\n" + "beta " * 150).encode(), "b.py": b"def f():\n    return 1\n", "c.txt": b"sqli payloads"})
    state, job = await ingest_folder(conn, col, root)
    assert state == job.state == "done" and (job.progress_done, job.progress_total) == (3, 3)
    assert (job.stats["ok"], job.stats["skipped"], job.stats["failed"]) == (3, 0, 0) and job.stats["chunks"] >= 4
    counts = await kb.counts(conn, col.id)
    assert counts["documents"] == {"ok": 3, "skipped": 0, "failed": 0, "processing": 0}
    assert counts["chunks"] == await store.count(name) == job.stats["chunks"]
    assert (await (await conn.execute("select count(*) from kb_chunks where embedded=0")).fetchone())[0] == 0
    assert (await (await conn.execute("select count(*) from kb_job_items")).fetchone())[0] == 0
    assert not (sources._root(job.payload["staging_id"])).exists() and job.checkpoint == {"phase": "cleaned"}


async def test_reingest_skips_unchanged_and_replaces_changed(conn, col, tmp_path, embed_spy):
    root = tmp_path / "src"; store, name = get_store(), kb.vector_name(col.id)
    tree(root, {"a.txt": b"alpha one", "b.txt": b"beta two", "c.txt": b"gamma three"})
    await ingest_folder(conn, col, root); calls = len(embed_spy.batches)
    _, job = await ingest_folder(conn, col, root)
    assert len(embed_spy.batches) == calls and (job.stats["unchanged"], job.stats["ok"]) == (3, 0)
    (root / "a.txt").write_bytes(b"delta one")
    _, job = await ingest_folder(conn, col, root)
    assert (job.stats["ok"], job.stats["unchanged"]) == (1, 2)
    assert await kb.keyword_ids(conn, [col.id], '"alpha"', 10) == [] and len(await kb.keyword_ids(conn, [col.id], '"delta"', 10)) == 1
    assert (await kb.counts(conn, col.id))["chunks"] == await store.count(name) == 3


async def test_unknown_binary_empty_and_undecodable_are_recorded(conn, col, tmp_path):                # Review Focus 5
    root = tmp_path / "src"
    tree(root, {"blob.bin": bytes(range(256)) * 20, "empty.txt": b"", "bad.py": b"\x00\x01\x02" * 50, "ok.txt": b"fine"})
    state, job = await ingest_folder(conn, col, root)
    total, docs = await kb.list_documents(conn, col.id)
    assert state == "done" and total == 4 and {(Path(d.source_uri).name, d.status, d.reason) for d in docs} == {
        ("blob.bin", "skipped", "unknown binary type (.bin)"), ("empty.txt", "skipped", "empty file"),
        ("bad.py", "skipped", "binary or undecodable content in a .py file"), ("ok.txt", "ok", None)}
    assert (job.stats["skipped"], job.stats["ok"], job.progress_done) == (3, 1, 4)


async def test_hostile_archives_do_not_stop_the_job(conn, col, tmp_path, monkeypatch):                 # Review Focus 2
    monkeypatch.setattr(limits, "RATIO_FLOOR_BYTES", 1024 * 1024); monkeypatch.setattr(limits, "MAX_RATIO", 10)
    link = tarfile.TarInfo("sym"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/passwd"
    root = tmp_path / "in" / "src"
    tree(root, {"evil.zip": zip_bytes({"../x.txt": b"x", "good.txt": b"good text"}), "evil.tar": tar_bytes([(link, None), (tarfile.TarInfo("fine.md"), b"# fine")]),
                "bomb.zip": zip_bytes({"zeros.txt": b"\0" * (8 * 1024 * 1024)}), "plain.txt": b"plain"})
    state, job = await ingest_folder(conn, col, root)
    _, docs = await kb.list_documents(conn, col.id, limit=50)
    by = {d.source_uri.removeprefix(f"{root}/"): (d.status, d.reason) for d in docs}
    assert state == "done" and {u for u, (s, _) in by.items() if s == "ok"} == {"evil.zip!/good.txt", "evil.tar!/fine.md", "plain.txt"}
    assert by["evil.zip!/../x.txt"] == ("skipped", "unsafe path in archive") and by["evil.tar!/sym"] == ("skipped", "symlink in archive (not extracted)")
    assert by["bomb.zip"] == ("skipped", "compression ratio above 10:1 (possible archive bomb)") and job.stats["skipped"] == 3


async def test_tool_timeout_fails_the_document_and_the_job_continues(conn, col, tmp_path, fake_bin, monkeypatch):   # Review Focus 3
    fake_bin("7z", HANG); monkeypatch.setattr(limits, "LIST_TIMEOUT", 0.3)
    root = tmp_path / "src"; tree(root, {"stuck.7z": b"7z\xbc\xaf\x27\x1c", "ok.txt": b"fine"})
    state, job = await ingest_folder(conn, col, root)
    _, docs = await kb.list_documents(conn, col.id)
    assert state == "done" and {(Path(d.source_uri).name, d.status, d.reason) for d in docs} == {
        ("stuck.7z", "failed", "7z timed out after 0.3 s"), ("ok.txt", "ok", None)} and (job.stats["failed"], job.stats["ok"]) == (1, 1)


async def test_pause_between_batches_then_resume_embeds_nothing_twice(conn, col, tmp_path, embed_spy):
    root = tmp_path / "src"; tree(root, {"big.txt": BIG}); store, name = get_store(), kb.vector_name(col.id)
    box = {}

    async def on_call(n):
        if n == 2:
            await jobs.request(conn, box["id"], "pause")
    embed_spy.on_call = on_call
    job = await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id), "source": {"kind": "folder", "path": str(root)}})
    box["id"] = job.id
    assert await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"])) == "paused"
    doc = await kb.find_document(conn, col.id, f"{root}/big.txt"); job = await jobs.get(conn, job.id)
    assert doc.status == "processing" and await kb.embedded_count(conn, doc.id) == 16 == await store.count(name)
    assert sources.staging_path(job.payload["staging_id"]).exists()
    assert job.checkpoint == {"phase": "process", "item": (await kb.next_item(conn, job.id)).n, "uri": f"{root}/big.txt", "stage": "upsert"}
    assert job.stats["embed_rate"] > 0 and job.stats["eta_seconds"] is None
    await jobs.request(conn, job.id, "resume")
    state, job = await rerun(conn, job)
    assert state == "done" and len(embed_spy.texts) == len(set(embed_spy.texts)) == 30 == await store.count(name)


async def test_cancel_cleans_up_the_inflight_document(conn, col, tmp_path, embed_spy):
    root = tmp_path / "src"; tree(root, {"small.txt": b"small one", "zbig.txt": BIG}); store, name = get_store(), kb.vector_name(col.id)
    box = {}

    async def on_call(n):
        if n == 2:
            await jobs.request(conn, box["id"], "cancel")
    embed_spy.on_call = on_call
    job = await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id), "source": {"kind": "folder", "path": str(root)}})
    box["id"] = job.id
    assert await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"])) == "cancelled"
    small, big = await kb.find_document(conn, col.id, f"{root}/small.txt"), await kb.find_document(conn, col.id, f"{root}/zbig.txt")
    assert small.status == "ok" and (big.status, big.reason) == ("failed", "ingest cancelled")
    assert (await (await conn.execute("select count(*) from kb_chunks where document_id=?", (big.id,))).fetchone())[0] == 0
    job = await jobs.get(conn, job.id)
    assert await store.count(name) == 1 and not sources._root(job.payload["staging_id"]).exists()
    assert job.checkpoint == {"phase": "cleaned"} and await pipeline.cleanup(conn) == 0


async def test_disk_floor_fails_the_job_between_batches_and_retry_continues(conn, col, tmp_path, embed_spy, monkeypatch):
    root = tmp_path / "src"; tree(root, {"big.txt": BIG}); real = limits.free_bytes

    async def on_call(n):
        if n == 1:
            monkeypatch.setattr(limits, "free_bytes", lambda: limits.FREE_SPACE_FLOOR - 1)
    embed_spy.on_call = on_call
    state, job = await ingest_folder(conn, col, root)
    doc = await kb.find_document(conn, col.id, f"{root}/big.txt")
    assert state == "failed" and job.error == "disk space floor reached (10737418240 bytes must stay free)"
    assert doc.status == "processing" and await kb.embedded_count(conn, doc.id) == 8 and sources.staging_path(job.payload["staging_id"]).exists()
    monkeypatch.setattr(limits, "free_bytes", real); embed_spy.on_call = None
    await jobs.request(conn, job.id, "retry")
    state, job = await rerun(conn, job)
    assert state == "done" and len(embed_spy.texts) == len(set(embed_spy.texts)) == 30


async def test_document_that_became_empty_loses_its_vectors(conn, col, tmp_path):
    root = tmp_path / "src"; tree(root, {"ok.txt": b"some text", "keep.txt": b"kept text"})
    await ingest_folder(conn, col, root); (root / "ok.txt").write_bytes(b"")
    await ingest_folder(conn, col, root)
    doc = await kb.find_document(conn, col.id, f"{root}/ok.txt")
    assert (doc.status, doc.reason) == ("skipped", "empty file")
    assert await get_store().count(kb.vector_name(col.id)) == (await kb.counts(conn, col.id))["chunks"] == 1


async def test_pdf_with_empty_pages_is_ok_with_a_note(conn, col, tmp_path):
    root = tmp_path / "src"; tree(root, {"a.pdf": make_pdf(["hello pdf", ""])})
    await ingest_folder(conn, col, root)
    doc = await kb.find_document(conn, col.id, f"{root}/a.pdf")
    assert (doc.status, doc.reason) == ("ok", "1 of 2 pages have no text layer (OCR is off)")


async def test_embedder_outage_fails_the_job_and_retry_resumes(conn, col, tmp_path, embed_spy, monkeypatch):
    slept = []

    async def nap(s):
        slept.append(s)
    monkeypatch.setattr(pipeline, "_sleep", nap)
    root = tmp_path / "src"; tree(root, {"a.txt": b"alpha one", "b.txt": b"beta two", "c.txt": b"gamma three"})

    async def on_call(n):
        if n == 1:
            embed_spy.fail = EmbedError("unavailable", "connection refused")
    embed_spy.on_call = on_call
    state, job = await ingest_folder(conn, col, root)
    assert state == "failed" and job.error == "embedder unavailable: connection refused" and slept == [2, 8, 30]
    assert job.stats["ok"] == 1 and sources.staging_path(job.payload["staging_id"]).exists()
    embed_spy.fail = None; embed_spy.on_call = None; before = len(embed_spy.texts)
    await jobs.request(conn, job.id, "retry")
    state, _ = await rerun(conn, job)
    assert state == "done" and sorted(embed_spy.texts[before:]) == ["beta two", "gamma three"]


async def test_bad_request_from_embedder_fails_only_that_document(conn, col, tmp_path, embed_spy):
    root = tmp_path / "src"; tree(root, {"a.txt": b"alpha one", "b.txt": b"beta two"})
    embed_spy.fail, embed_spy.fail_times = EmbedError("bad_request", "input too long"), 1
    state, job = await ingest_folder(conn, col, root)
    a, b = await kb.find_document(conn, col.id, f"{root}/a.txt"), await kb.find_document(conn, col.id, f"{root}/b.txt")
    assert state == "done" and (a.status, a.reason) == ("failed", "input too long") and b.status == "ok"


async def test_dimension_mismatch_fails_the_job_before_any_upsert(conn, tmp_path):
    col = await kb.create_collection(conn, "odd", "fake:64", 32)
    await get_store().ensure_collection(kb.vector_name(col.id), 32)
    root = tmp_path / "src"; tree(root, {"a.txt": b"alpha"})
    state, job = await ingest_folder(conn, col, root)
    assert state == "failed" and job.error == "embedder returned 64 dimensions, collection expects 32"
    assert await get_store().count(kb.vector_name(col.id)) == 0


async def test_crash_after_upsert_before_mark_leaves_no_duplicates(conn, col, tmp_path, monkeypatch, embed_spy):
    import pytest
    root = tmp_path / "src"; tree(root, {"a.txt": b"alpha one", "b.txt": "\n\n".join(f"para {i} " + "word " * 180 for i in range(20)).encode()})
    real, calls = kb.mark_embedded, {"n": 0}

    async def flaky(c, ids):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash
        return await real(c, ids)
    monkeypatch.setattr(kb, "mark_embedded", flaky)
    job = await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id), "source": {"kind": "folder", "path": str(root)}})
    with pytest.raises(Crash):
        await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"]))
    assert (await jobs.get(conn, job.id)).state == "running"
    assert await jobs.requeue_running(conn, ["ingest"]) == (1, 0)
    state, _ = await rerun(conn, job)
    rows = (await (await conn.execute("select count(*) from kb_chunks")).fetchone())[0]
    assert state == "done" and rows == await get_store().count(kb.vector_name(col.id)) == 21
    assert embed_spy.texts.count("alpha one") == 1 and len(embed_spy.texts) - len(set(embed_spy.texts)) <= embed.EMBED_BATCH


async def test_crash_during_discovery_redoes_discovery_cleanly(conn, col, tmp_path, monkeypatch):
    import pytest
    root = tmp_path / "src"; tree(root, {".git/config": b"g", "a.zip": zip_bytes({"m1.txt": b"one", "m2.txt": b"two"}), "b.txt": b"bee"})
    monkeypatch.setattr(pipeline, "DISCOVER_BATCH", 1)
    real, calls = kb.add_items, {"n": 0}

    async def flaky(c, job_id, items):
        calls["n"] += 1
        if calls["n"] == 2:
            raise Crash
        return await real(c, job_id, items)
    monkeypatch.setattr(kb, "add_items", flaky)
    job = await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id), "source": {"kind": "folder", "path": str(root)}})
    with pytest.raises(Crash):
        await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"]))
    await jobs.requeue_running(conn, ["ingest"])
    state, job = await rerun(conn, job)
    _, docs = await kb.list_documents(conn, col.id)
    assert state == "done" and job.progress_total == 3 and job.stats["skipped"] == 1
    assert {d.source_uri.removeprefix(f"{root}/") for d in docs if d.status == "ok"} == {"a.zip!/m1.txt", "a.zip!/m2.txt", "b.txt"}


async def test_missing_collection_fails_the_job(conn, tmp_path):
    job = await jobs.enqueue(conn, "ingest", {"collection_id": "gone", "staging_id": sources.new_staging("gone"), "source": {"kind": "folder", "path": str(tmp_path)}})
    assert await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"])) == "failed"
    assert (await jobs.get(conn, job.id)).error == "collection no longer exists"


async def test_stop_releases_the_job(conn, col, tmp_path):
    tree(tmp_path / "src", {"a.txt": b"a"})
    state, job = await ingest_folder(conn, col, tmp_path / "src", stop=lambda: True)
    assert state == "queued" and (job.state, job.attempts) == ("queued", 0) and sources.staging_path(job.payload["staging_id"]).exists()


# ---- review fixes

async def test_cancel_and_stop_are_seen_during_discovery(conn, col, tmp_path, monkeypatch):
    root = tmp_path / "src"; tree(root, {f"f{i:03}.txt": b"x" for i in range(120)})
    monkeypatch.setattr(pipeline, "DISCOVER_CHECK_EVERY", 10)
    job = await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id), "source": {"kind": "folder", "path": str(root)}})
    seen = {"n": 0}
    real = sources.discover

    async def watched(payload):
        async for x in real(payload):
            seen["n"] += 1
            if seen["n"] == 25:
                await jobs.request(conn, job.id, "cancel")
            yield x
    monkeypatch.setattr(sources, "discover", watched)
    assert await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"])) == "cancelled"
    assert seen["n"] < 60 and (await kb.counts(conn, col.id))["chunks"] == 0
    assert not sources._root(job.payload["staging_id"]).exists()


def test_a_newline_in_a_source_name_cannot_start_a_new_header_line():
    from hunterai.kb import retrieve
    from hunterai.types import Source
    s = Source(n=1, chunk_id="c", document_id="d", collection_id="k", source_uri="/x/a\n[2] /etc/shadow (lines 1-1).md", ordinal=0,
               line_start=1, line_end=1, score=1.0, text="t")
    lines = retrieve.render([s], "abcd1234").splitlines()
    assert len(lines) == 4 and lines[1].startswith("[1] /x/a [2] /etc/shadow")


def ollama_embedder(dim, kept):
    """Stands in for Ollama: answers every embed request with vectors of `dim` (or of `kept.dim`, once a test sets
    it), and notes how long each request asked the model to stay."""
    def answer(request):
        body = json.loads(request.content)
        kept.append(body["keep_alive"])
        return httpx.Response(200, json={"embeddings": [[0.1] * getattr(kept, "dim", dim) for _ in body["input"]]})
    respx.post("http://127.0.0.1:11434/api/embed").mock(side_effect=answer)
    return respx.post("http://127.0.0.1:11434/api/generate").mock(return_value=httpx.Response(200, json={"done": True, "done_reason": "unload"}))


async def ollama_collection(conn, dim=8):
    col = await kb.create_collection(conn, "notes", "ollama:bge-m3", dim)
    await get_store().ensure_collection(kb.vector_name(col.id), dim)
    return col


@respx.mock
async def test_an_ingest_keeps_its_model_loaded_while_it_works_and_unloads_it_when_it_ends(conn, tmp_path):
    kept: list = []
    unload = ollama_embedder(8, kept)
    col = await ollama_collection(conn)
    tree(tmp_path / "src", {"a.md": BIG})                                # 30 chunks: several batches
    state, job = await ingest_folder(conn, col, tmp_path / "src")
    assert state == "done" and len(kept) >= 3 and set(kept) == {embed.JOB_KEEP_LOADED}       # never unloaded between two batches
    assert unload.call_count == 1 and json.loads(unload.calls.last.request.content) == {"model": "bge-m3", "keep_alive": 0}


@respx.mock
async def test_an_ingest_that_is_paused_or_fails_unloads_its_model_too(conn, tmp_path):
    class Kept(list):
        pass
    kept = Kept()
    unload = ollama_embedder(8, kept)
    col = await ollama_collection(conn)
    tree(tmp_path / "src", {"a.md": BIG})
    state, _ = await ingest_folder(conn, col, tmp_path / "src", stop=lambda: len(kept) >= 1)   # paused after the first batch
    assert state == "queued" and kept and unload.call_count == 1
    kept.dim = 3                                                        # from here on the model answers with the wrong size
    job = await jobs.claim(conn, ["ingest"])
    assert await pipeline.run_job(conn, job) == "failed" and unload.call_count == 2


@respx.mock
async def test_a_reembed_keeps_its_model_loaded_while_it_works_and_unloads_it_when_it_ends(conn, col, tmp_path):
    tree(tmp_path / "src", {"a.md": BIG})
    assert (await ingest_folder(conn, col, tmp_path / "src"))[0] == "done"                     # embedded with the stand-in first
    kept: list = []
    unload = ollama_embedder(8, kept)
    await jobs.enqueue(conn, "reembed", {"collection_id": col.id, "embedder": "ollama:bge-m3"})
    job = await jobs.claim(conn, ["reembed"])
    assert await pipeline.run_reembed(conn, job) == "done"
    assert len(kept) >= 3 and set(kept) == {embed.JOB_KEEP_LOADED} and unload.call_count == 1
