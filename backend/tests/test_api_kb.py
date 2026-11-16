import json

import httpx
import respx

from hunterai import config, db, worker
from hunterai.kb import limits, vectors
from hunterai.kb.vectors import VectorError, get_store
from hunterai.repo import jobs, kb, settings
from tests.kb_samples import tree, zip_bytes
from tests.test_api import client, new_chat  # noqa: F401

EMBED_URL = "http://127.0.0.1:11434/api/embed"


async def new_col(client, name="Notes"):
    r = await client.post("/api/kb/collections", json={"name": name, "embedder": "fake:64"})
    assert r.status_code == 200, r.text
    return r.json()


async def test_collection_crud_reads_dimension_from_the_embedder(client):
    r = await client.post("/api/kb/collections", json={"name": "  Notes ", "embedder": "fake:48"})
    col = r.json()
    assert (col["name"], col["dimension"], col["embedder"]) == ("Notes", 48, "fake:48")
    assert (await client.post("/api/kb/collections", json={"name": "notes", "embedder": "fake:48"})).status_code == 409
    assert (await client.post("/api/kb/collections", json={"name": "x", "embedder": "openai:ada"})).status_code == 400
    assert (await client.post("/api/kb/collections", json={"name": " ", "embedder": "fake:48"})).status_code == 400
    assert (await client.patch(f"/api/kb/collections/{col['id']}", json={"name": "Field"})).json()["name"] == "Field"
    assert [c["name"] for c in (await client.get("/api/kb/collections")).json()] == ["Field"]
    assert (await client.delete(f"/api/kb/collections/{col['id']}")).status_code == 204
    assert (await client.get("/api/kb/collections")).json() == []


@respx.mock
async def test_default_embedder_is_bge_m3_and_an_outage_creates_nothing(client):
    route = respx.post(EMBED_URL).mock(side_effect=httpx.ConnectError("down"))
    assert (await client.post("/api/kb/collections", json={"name": "Live"})).status_code == 503
    assert (await client.get("/api/kb/collections")).json() == []
    route.mock(return_value=httpx.Response(200, json={"embeddings": [[0.1] * 1024]}))
    col = (await client.post("/api/kb/collections", json={"name": "Live"})).json()
    assert (col["embedder"], col["dimension"]) == ("ollama:bge-m3", 1024)


async def test_upload_ingest_flow_keeps_relative_paths(client, conn):
    cid = (await new_col(client))["id"]; up = (await client.post(f"/api/kb/collections/{cid}/uploads")).json()["upload_id"]
    packed = zip_bytes({"in.txt": b"zipped text", "../up.txt": b"x"})
    r = await client.post(f"/api/kb/uploads/{up}/files", data={"paths": ["notes/a.md", "bin/blob.bin", "../../e.txt", "pack/a.zip"]},
                          files=[("files", ("a.md", b"# A\n\nalpha text", "text/markdown")), ("files", ("blob", bytes(range(256)) * 4, "application/octet-stream")),
                                 ("files", ("e.txt", b"evil", "text/plain")), ("files", ("a.zip", packed, "application/zip"))])
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 3 and r.json()["rejected"] == [{"path": "../../e.txt", "reason": "unsafe path"}]
    job = (await client.post(f"/api/kb/uploads/{up}/commit")).json()
    assert (job["state"], job["kind"], job["payload"]["source"]) == ("queued", "ingest", {"kind": "upload"})
    assert (await client.post(f"/api/kb/uploads/{up}/commit")).status_code == 409 and await worker.drain(conn) == 1
    docs = (await client.get(f"/api/kb/collections/{cid}/documents")).json()
    assert docs["total"] == 5 and {(d["source_uri"], d["status"]) for d in docs["items"]} == {
        ("upload:notes/a.md", "ok"), ("upload:bin/blob.bin", "skipped"), ("upload:../../e.txt", "skipped"),
        ("upload:pack/a.zip!/in.txt", "ok"), ("upload:pack/a.zip!/../up.txt", "skipped")}
    assert not (config.staging_dir() / up).exists()
    st = (await client.get(f"/api/kb/collections/{cid}/status")).json()
    assert st["documents"] == {"ok": 2, "skipped": 3, "failed": 0, "processing": 0} and st["chunks"] == st["vectors"] >= 2
    assert (st["dimension"], st["embedder"], st["active_job"]) == (64, "fake:64", None)
    skipped = (await client.get(f"/api/kb/collections/{cid}/documents?status=skipped")).json()
    assert skipped["total"] == 3 and all(d["reason"] for d in skipped["items"])
    assert (await client.get(f"/api/kb/jobs?collection_id={cid}")).json()[0]["state"] == "done"


async def test_upload_request_limits(client, monkeypatch):
    cid = (await new_col(client))["id"]; up = (await client.post(f"/api/kb/collections/{cid}/uploads")).json()["upload_id"]
    many = [("files", (f"f{i}.txt", b"x", "text/plain")) for i in range(101)]
    assert (await client.post(f"/api/kb/uploads/{up}/files", data={"paths": [f"f{i}.txt" for i in range(101)]}, files=many)).status_code == 400
    two = [("files", ("a.txt", b"a", "text/plain")), ("files", ("b.txt", b"b", "text/plain"))]
    assert (await client.post(f"/api/kb/uploads/{up}/files", data={"paths": ["a.txt"]}, files=two)).status_code == 400
    assert (await client.post(f"/api/kb/uploads/{up}/commit")).status_code == 400
    assert (await client.post("/api/kb/uploads/not-an-id/files", data={"paths": ["a.txt"]}, files=two[:1])).status_code == 404
    real = limits.MAX_REQUEST_BYTES
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", 1000)
    big = [("files", ("big.txt", b"x" * 5000, "text/plain"))]
    assert (await client.post(f"/api/kb/uploads/{up}/files", data={"paths": ["big.txt"]}, files=big)).status_code == 413
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", real)
    r = await client.post(f"/api/kb/uploads/{up}/files", data={"paths": ["ok.txt"], "too_large": ["big/video.bin"]}, files=[("files", ("ok.txt", b"ok", "text/plain"))])
    assert r.json()["accepted"] == 1 and r.json()["rejected"][0]["path"] == "big/video.bin" and r.json()["files"] == 1
    monkeypatch.setattr(limits, "free_bytes", lambda: limits.FREE_SPACE_FLOOR - 1)
    assert (await client.post(f"/api/kb/collections/{cid}/uploads")).status_code == 507


async def test_folder_ingest_validation(client, tmp_path):
    cid = (await new_col(client))["id"]; url = f"/api/kb/collections/{cid}/ingest/folder"
    inside = config.home() / "work"; inside.mkdir(parents=True, exist_ok=True)
    for bad in ("relative/path", str(tmp_path / "missing"), str(config.home()), str(inside)):
        assert (await client.post(url, json={"path": bad})).status_code == 400, bad
    tree(tmp_path / "src", {"a.txt": b"a"})
    job = (await client.post(url, json={"path": str(tmp_path / "src")})).json()
    assert job["payload"]["source"] == {"kind": "folder", "path": str((tmp_path / "src").resolve())} and job["state"] == "queued"


async def test_job_controls_and_conflicts(client, tmp_path):
    cid = (await new_col(client))["id"]; tree(tmp_path / "src", {"a.txt": b"a"})
    job = (await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src")})).json()
    act = lambda a, j=job["id"]: client.post(f"/api/kb/jobs/{j}/{a}")  # noqa: E731
    assert (await act("pause")).json()["state"] == "paused"
    r = await act("pause"); assert r.status_code == 409 and r.json()["detail"] == "cannot pause a paused job"
    assert (await act("resume")).json()["state"] == "queued" and (await act("cancel")).json()["state"] == "cancelled"
    assert (await act("retry")).status_code == 409 and (await act("explode")).status_code == 400
    assert (await act("pause", "nope")).status_code == 404 and (await client.get(f"/api/kb/jobs/{job['id']}")).json()["state"] == "cancelled"


async def test_delete_collection_is_blocked_by_an_active_job(client, conn, tmp_path):
    cid = (await new_col(client))["id"]; tree(tmp_path / "src", {"a.txt": b"a"})
    failed = (await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src")})).json()
    await conn.execute("update jobs set state='failed' where id=?", (failed["id"],)); await conn.commit()
    await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src")})
    r = await client.delete(f"/api/kb/collections/{cid}")
    assert r.status_code == 409 and r.json()["detail"] == "cancel its jobs first"
    await worker.drain(conn)
    assert (await client.delete(f"/api/kb/collections/{cid}")).status_code == 204
    import pytest
    with pytest.raises(VectorError):
        await get_store().count(kb.vector_name(cid))
    assert (await jobs.get(conn, failed["id"])).state == "cancelled"


async def test_collection_row_is_removed_when_the_vector_store_fails(client, monkeypatch):
    async def boom(self, name, dim):
        raise VectorError("vector store unavailable: ConnectError")
    monkeypatch.setattr(vectors.MemoryStore, "ensure_collection", boom)
    assert (await client.post("/api/kb/collections", json={"name": "X", "embedder": "fake:8"})).status_code == 503
    assert (await client.get("/api/kb/collections")).json() == []


async def test_search_endpoint(client, conn, tmp_path):
    cid = (await new_col(client))["id"]
    tree(tmp_path / "src", {"sqli.md": b"union select sqli payload for mysql", "xss.md": b"script alert xss payload", "heap.md": b"kernel heap spray"})
    await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src")}); await worker.drain(conn)
    r = (await client.post("/api/kb/search", json={"collection_ids": [cid], "query": "union sqli", "limit": 2})).json()
    assert len(r["hits"]) == 2 and "sqli" in r["hits"][0]["text"] and r["duration_ms"] >= 0
    assert {"score", "source_uri", "ordinal", "dense_rank", "keyword_rank", "line_start", "line_end"} <= set(r["hits"][0])
    assert (await client.post("/api/kb/search", json={"collection_ids": [cid], "query": '"a AND ('})).status_code == 200
    assert (await client.post("/api/kb/search", json={"collection_ids": ["nope"], "query": "x"})).status_code == 404
    assert (await client.post("/api/kb/search", json={"collection_ids": [cid], "query": "x", "limit": 0})).status_code == 422


async def test_chat_attach_and_toggle(client):
    cid = (await new_col(client))["id"]; chat = await new_chat(client)
    url = f"/api/kb/chats/{chat['id']}"
    assert (await client.get(url)).json() == {"chat_id": chat["id"], "collection_ids": [], "always_retrieve": False}
    assert (await client.put(url, json={"collection_ids": [cid], "always_retrieve": True})).json()["collection_ids"] == [cid]
    got = (await client.put(url, json={"always_retrieve": False})).json()
    assert (got["collection_ids"], got["always_retrieve"]) == ([cid], False)
    assert (await client.put(url, json={"collection_ids": ["nope"]})).status_code == 404
    assert (await client.get("/api/kb/chats/nope")).status_code == 404
    await client.delete(f"/api/kb/collections/{cid}")
    assert (await client.get(url)).json()["collection_ids"] == []


async def test_health_and_running_job_status(client, conn, tmp_path):
    assert (await client.get("/api/kb/health")).json() == {"worker_alive": False, "vector_backend": "memory"}
    await settings.put(conn, "worker_heartbeat", db.now())
    assert (await client.get("/api/kb/health")).json()["worker_alive"] is True
    cid = (await new_col(client))["id"]; tree(tmp_path / "src", {"a.txt": b"a"})
    job = (await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src")})).json()
    await conn.execute("update jobs set state='running', stats_json='{\"embed_rate\": 4.5, \"eta_seconds\": 120}' where id=?", (job["id"],)); await conn.commit()
    st = (await client.get(f"/api/kb/collections/{cid}/status")).json()
    assert (st["embed_rate"], st["eta_seconds"], st["active_job"]["id"]) == (4.5, 120, job["id"])


async def test_host_header_is_still_enforced(client):
    assert (await client.get("/api/kb/collections", headers={"host": "evil.example"})).status_code == 403


@respx.mock
async def test_text_embedded_by_the_api_leaves_no_model_loaded_behind():
    from types import SimpleNamespace

    from hunterai.api.kb import _embed
    from hunterai.kb import embed
    kept: list = []

    def answer(request):
        body = json.loads(request.content)
        kept.append(body["keep_alive"])
        return httpx.Response(200, json={"embeddings": [[0.1] for _ in body["input"]]})
    respx.post(EMBED_URL).mock(side_effect=answer)
    unload = respx.post("http://127.0.0.1:11434/api/generate").mock(return_value=httpx.Response(200, json={"done": True, "done_reason": "unload"}))
    col = SimpleNamespace(embedder="ollama:bge-m3", dimension=1)
    await _embed(col, ["one text"])
    assert kept == [0] and not unload.called                         # one request unloads the model with its own answer
    await _embed(col, [f"text {i}" for i in range(20)])              # three requests: loaded once, unloaded once
    assert kept[1:] == [embed.JOB_KEEP_LOADED] * 3 and unload.call_count == 1
