from hunterai import worker
from hunterai.kb.vectors import get_store
from hunterai.repo import jobs, kb
from tests.kb_samples import tree
from tests.test_api import client  # noqa: F401


async def setup(client, conn, tmp_path, files=None):
    cid = (await client.post("/api/kb/collections", json={"name": "Notes", "embedder": "fake:64"})).json()["id"]
    tree(tmp_path / "src", files or {"a.txt": b"alpha payload one", "b.txt": b"beta payload two"})
    await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src")})
    await worker.drain(conn)
    docs = (await client.get(f"/api/kb/collections/{cid}/documents")).json()["items"]
    return cid, {d["source_uri"].rsplit("/", 1)[-1]: d for d in docs}


async def search(client, cid, q):
    return [h["text"] for h in (await client.post("/api/kb/search", json={"collection_ids": [cid], "query": q})).json()["hits"]]


async def test_open_edit_and_delete_a_chunk(client, conn, tmp_path):
    cid, docs = await setup(client, conn, tmp_path)
    got = (await client.get(f"/api/kb/documents/{docs['a.txt']['id']}/chunks")).json()
    assert got["total"] == 1 and got["items"][0]["text"] == "alpha payload one" and got["document"]["source_uri"].endswith("a.txt")
    chunk = got["items"][0]["id"]
    edited = (await client.patch(f"/api/kb/chunks/{chunk}", json={"text": "  gamma corrected note  "})).json()
    assert edited["text"] == "gamma corrected note"
    assert (await search(client, cid, "gamma corrected"))[0] == "gamma corrected note"
    assert "alpha payload one" not in await search(client, cid, "alpha")              # keyword and vector both follow the edit
    assert (await client.patch(f"/api/kb/chunks/{chunk}", json={"text": " "})).status_code == 400
    assert (await client.patch(f"/api/kb/chunks/{chunk}", json={"text": "x" * 2001})).status_code == 400
    assert (await client.patch("/api/kb/chunks/nope", json={"text": "x"})).status_code == 404
    assert (await client.delete(f"/api/kb/chunks/{chunk}")).status_code == 204
    st = (await client.get(f"/api/kb/collections/{cid}/status")).json()
    assert st["chunks"] == st["vectors"] == 1 and await search(client, cid, "gamma corrected") == ["beta payload two"]
    assert (await kb.get_document(conn, docs["a.txt"]["id"])).chunk_count == 0


async def test_delete_a_document_and_add_a_note(client, conn, tmp_path):
    cid, docs = await setup(client, conn, tmp_path)
    assert (await client.delete(f"/api/kb/documents/{docs['b.txt']['id']}")).status_code == 204
    assert await get_store().count(kb.vector_name(cid)) == 1 and (await client.delete("/api/kb/documents/nope")).status_code == 404
    note = (await client.post(f"/api/kb/collections/{cid}/notes", json={"title": "  SSRF  trick ", "text": "use the image proxy for ssrf"})).json()
    assert (note["source_uri"], note["source_kind"], note["status"], note["chunk_count"]) == ("manual:SSRF trick", "manual", "ok", 1)
    assert (await search(client, cid, "image proxy ssrf"))[0] == "use the image proxy for ssrf"
    again = (await client.post(f"/api/kb/collections/{cid}/notes", json={"title": "SSRF trick", "text": "replaced text about gopher"})).json()
    assert again["id"] == note["id"] and await get_store().count(kb.vector_name(cid)) == 2
    assert "use the image proxy for ssrf" not in await search(client, cid, "image proxy")
    assert (await client.post(f"/api/kb/collections/{cid}/notes", json={"title": "", "text": "x"})).status_code == 400


async def test_edits_wait_while_a_job_is_active(client, conn, tmp_path):
    cid, docs = await setup(client, conn, tmp_path)
    chunk = (await client.get(f"/api/kb/documents/{docs['a.txt']['id']}/chunks")).json()["items"][0]["id"]
    await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src"), "again": True})      # queued (the folder is known: asked for again)
    for r in (await client.patch(f"/api/kb/chunks/{chunk}", json={"text": "x"}), await client.delete(f"/api/kb/chunks/{chunk}"),
              await client.delete(f"/api/kb/documents/{docs['a.txt']['id']}"), await client.post(f"/api/kb/collections/{cid}/notes", json={"title": "t", "text": "x"}),
              await client.post(f"/api/kb/collections/{cid}/reembed", json={"embedder": "fake:32"})):
        assert r.status_code == 409


async def test_reembed_changes_the_embedder_and_every_vector(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path, {f"d{i}.txt": f"note number {i} about sqli".encode() for i in range(20)})
    assert (await client.post(f"/api/kb/collections/{cid}/reembed", json={"embedder": "openai:x"})).status_code == 400
    job = (await client.post(f"/api/kb/collections/{cid}/reembed", json={"embedder": "fake:32"})).json()
    assert job["kind"] == "reembed" and (await client.get(f"/api/kb/jobs?collection_id={cid}")).json()[0]["id"] == job["id"]
    assert await worker.drain(conn) == 1
    done = await jobs.get(conn, job["id"])
    assert (done.state, done.progress_done, done.progress_total, done.stats["chunks"]) == ("done", 20, 20, 20)
    st = (await client.get(f"/api/kb/collections/{cid}/status")).json()
    assert (st["embedder"], st["dimension"], st["chunks"], st["vectors"]) == ("fake:32", 32, 20, 20)
    assert len(await search(client, cid, "sqli note number 7")) == 8


async def test_reembed_resumes_after_a_pause(client, conn, tmp_path, embed_spy):
    cid, _ = await setup(client, conn, tmp_path, {f"d{i}.txt": f"note number {i}".encode() for i in range(20)})
    job = (await client.post(f"/api/kb/collections/{cid}/reembed", json={"embedder": "fake:32"})).json()
    start = len(embed_spy.batches)

    async def on_call(n):
        if n == start + 2:                        # the dimension probe, then the first batch
            await jobs.request(conn, job["id"], "pause")
    embed_spy.on_call = on_call
    await worker.drain(conn)
    paused = await jobs.get(conn, job["id"])
    assert paused.state == "paused" and paused.progress_done == 8 and paused.checkpoint == {"phase": "embed"}
    embed_spy.on_call = None
    await jobs.request(conn, job["id"], "resume"); await worker.drain(conn)
    assert (await jobs.get(conn, job["id"])).state == "done" and await get_store().count(kb.vector_name(cid)) == 20
    texts = [t for b in embed_spy.batches[start + 1:] for t in b]
    assert len(texts) == len(set(texts)) == 20                                  # nothing embedded twice


# ---- review fixes

async def test_cancel_before_a_reembed_starts_leaves_the_index_alone(client, conn, tmp_path, embed_spy):
    cid, _ = await setup(client, conn, tmp_path)
    job = (await client.post(f"/api/kb/collections/{cid}/reembed", json={"embedder": "fake:32"})).json()

    async def on_call(n):                       # the dimension probe is the first call
        embed_spy.on_call = None
        await jobs.request(conn, job["id"], "cancel")
    embed_spy.on_call = on_call
    await worker.drain(conn)
    st = (await client.get(f"/api/kb/collections/{cid}/status")).json()
    assert (await jobs.get(conn, job["id"])).state == "cancelled"
    assert (st["embedder"], st["dimension"], st["chunks"], st["vectors"]) == ("fake:64", 64, 2, 2)
    assert (await conn.execute_fetchall("select count(*) from kb_chunks where embedded=0"))[0][0] == 0


async def test_a_started_reembed_can_be_paused_but_not_cancelled(client, conn, tmp_path, embed_spy):
    cid, _ = await setup(client, conn, tmp_path, {f"d{i}.txt": f"note number {i}".encode() for i in range(20)})
    job = (await client.post(f"/api/kb/collections/{cid}/reembed", json={"embedder": "fake:32"})).json()
    start = len(embed_spy.batches)

    async def on_call(n):
        if n == start + 2:
            await jobs.request(conn, job["id"], "pause")
    embed_spy.on_call = on_call
    await worker.drain(conn)
    r = await client.post(f"/api/kb/jobs/{job['id']}/cancel")
    assert r.status_code == 409 and "cannot be cancelled" in r.json()["detail"]
    assert (await client.post(f"/api/kb/jobs/{job['id']}/resume")).status_code == 200


async def test_one_chunk_the_new_model_refuses_does_not_block_a_reembed(client, conn, tmp_path, monkeypatch):
    from hunterai.kb import embed
    from hunterai.kb.embed import EmbedError
    cid, _ = await setup(client, conn, tmp_path, {f"d{i}.txt": f"note number {i}".encode() for i in range(10)} | {"bad.txt": b"POISON text"})
    real = embed.FakeEmbedder.embed

    async def picky(self, texts):
        if self.dim == 32 and any("POISON" in t for t in texts):
            raise EmbedError("bad_request", "input too long")
        return await real(self, texts)
    monkeypatch.setattr(embed.FakeEmbedder, "embed", picky)
    job = (await client.post(f"/api/kb/collections/{cid}/reembed", json={"embedder": "fake:32"})).json()
    await worker.drain(conn)
    done = await jobs.get(conn, job["id"])
    st = (await client.get(f"/api/kb/collections/{cid}/status")).json()
    assert done.state == "done" and done.stats["failed"] == 1 and done.stats["last_error"] == "input too long"
    assert st["chunks"] == st["vectors"] == 10
