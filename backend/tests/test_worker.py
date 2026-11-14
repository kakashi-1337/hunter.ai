import asyncio
import fcntl
import hashlib
import os
import signal
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from hunterai import config, db, worker
from hunterai.kb import embed, pipeline, sources
from hunterai.kb.vectors import get_store
from hunterai.repo import jobs, kb, settings
from tests.kb_samples import age_staging, make_collection, tree

BACKEND = Path(__file__).resolve().parents[1]
WORKER = [sys.executable, "-m", "hunterai.worker"]
BIG = "\n\n".join(f"para {i} " + "word " * 180 for i in range(30)).encode()


def worker_env(**extra):      # a stray real embed call from a worker subprocess hits a closed port
    return {**os.environ, "OLLAMA_HOST": "http://127.0.0.1:9", **extra}


async def queue_folder(conn, col, root):
    return await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id),
                                               "source": {"kind": "folder", "path": str(root)}})


def thirty_docs(src):
    tree(src, {f"doc{i:02}.txt": "\n\n".join(f"doc{i} para{p} " + "word " * 180 for p in range(3)).encode() for i in range(30)})


async def wait_ok(conn, col, n, proc):
    for _ in range(300):
        if (await kb.counts(conn, col.id))["documents"]["ok"] >= n:
            return
        assert proc.poll() is None, "worker exited early"
        await asyncio.sleep(0.1)
    pytest.fail("worker made no progress")


async def test_sigkill_mid_ingest_then_restart_finishes_without_duplicates(conn, tmp_path):          # Review Focus 1
    src = tmp_path / "src"; thirty_docs(src)
    col = await make_collection(conn); name = kb.vector_name(col.id); store = get_store()
    job = await queue_folder(conn, col, src)
    log = tmp_path / "embed.log"; env = worker_env(HUNTERAI_FAKE_EMBED_LOG=str(log))
    proc = subprocess.Popen(WORKER, cwd=BACKEND, env={**env, "HUNTERAI_FAKE_EMBED_DELAY": "0.2"})
    try:
        await wait_ok(conn, col, 3, proc)
    finally:
        os.kill(proc.pid, signal.SIGKILL); proc.wait()
    killed = await jobs.get(conn, job.id); cp = killed.checkpoint
    assert killed.state == "running"                                                       # it really died mid-flight
    assert cp["phase"] == "process" and cp["stage"] in {"fetch", "clean", "chunk", "embed", "upsert"} and cp["uri"].startswith(str(src))
    finished = {d.id for d in (await kb.list_documents(conn, col.id, status="ok", limit=1000))[1]}
    assert 3 <= len(finished) < 30

    assert subprocess.run([*WORKER, "--once"], cwd=BACKEND, env=env, timeout=120).returncode == 0
    job = await jobs.get(conn, job.id)
    assert (job.state, job.attempts, job.progress_done, job.stats["ok"] + job.stats["unchanged"]) == ("done", 2, 30, 30)
    rows = await (await conn.execute("select id, document_id, ordinal, text, embedded from kb_chunks where collection_id=?", (col.id,))).fetchall()
    assert len(rows) == 90 == len({r["id"] for r in rows}) == len({(r["document_id"], r["ordinal"]) for r in rows})
    assert all(r["embedded"] for r in rows) and await store.count(name) == 90
    assert (await kb.counts(conn, col.id))["documents"] == {"ok": 30, "skipped": 0, "failed": 0, "processing": 0}
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
    seen = Counter(log.read_text().split())
    assert set(seen) == {sha(r["text"]) for r in rows}
    assert all(seen[sha(r["text"])] == 1 for r in rows if r["document_id"] in finished)     # finished documents never embedded again
    assert sum(n - 1 for n in seen.values()) <= embed.EMBED_BATCH                           # at most the one in-flight batch repeated
    assert len(await kb.keyword_ids(conn, [col.id], '"doc7"', 50)) == 3                     # no duplicates in the keyword index


async def test_drain_runs_queued_jobs_in_order(conn, col, tmp_path):
    tree(tmp_path / "a", {"a.txt": b"alpha"}); tree(tmp_path / "b", {"b.txt": b"beta"})
    first = await queue_folder(conn, col, tmp_path / "a"); second = await queue_folder(conn, col, tmp_path / "b")
    assert await worker.drain(conn) == 2
    f, s = await jobs.get(conn, first.id), await jobs.get(conn, second.id)
    assert f.state == s.state == "done" and f.finished_at <= s.started_at


async def test_main_once_resumes_a_job_left_running(conn, col, tmp_path):
    tree(tmp_path / "a", {"a.txt": b"alpha"})
    job = await queue_folder(conn, col, tmp_path / "a"); await jobs.claim(conn, ["ingest"])
    assert await worker.main(once=True) == 0
    job = await jobs.get(conn, job.id)
    assert (job.state, job.attempts) == ("done", 2)


def test_second_worker_refuses_to_start():
    config.home().mkdir(parents=True, exist_ok=True)
    fd = os.open(config.home() / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        r = subprocess.run([*WORKER, "--once"], cwd=BACKEND, env=worker_env(), capture_output=True, text=True, timeout=60)
    finally:
        os.close(fd)
    assert r.returncode == 3 and "another HunterAI worker is running" in r.stderr


async def test_sigterm_releases_the_job_and_exits_cleanly(conn, tmp_path):
    src = tmp_path / "src"; thirty_docs(src)
    col = await make_collection(conn); job = await queue_folder(conn, col, src)
    proc = subprocess.Popen(WORKER, cwd=BACKEND, env=worker_env(HUNTERAI_FAKE_EMBED_DELAY="0.2"))
    try:
        await wait_ok(conn, col, 1, proc)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
    j = await jobs.get(conn, job.id)
    assert (j.state, j.attempts) == ("queued", 0)
    assert subprocess.run([*WORKER, "--once"], cwd=BACKEND, env=worker_env(), timeout=120).returncode == 0
    j = await jobs.get(conn, job.id)
    assert (j.state, j.attempts) == ("done", 1)


async def test_heartbeat_makes_alive_true(conn):
    assert await worker.alive(conn) is False
    await settings.put(conn, "worker_heartbeat", db.now())
    assert await worker.alive(conn) is True
    await settings.put(conn, "worker_heartbeat", "2026-01-01T00:00:00+00:00")
    assert await worker.alive(conn) is False


async def test_startup_sweeps_orphan_staging_and_cleans_cancelled_jobs(conn, col, tmp_path):
    orphan = sources.new_staging(col.id); sources.mark_committed(orphan, "nobody"); age_staging(orphan, 120)
    tree(tmp_path / "a", {"a.txt": b"alpha"})
    cancelled = await queue_folder(conn, col, tmp_path / "a"); await jobs.request(conn, cancelled.id, "cancel")
    old, recent = await queue_folder(conn, col, tmp_path / "a"), await queue_folder(conn, col, tmp_path / "a")
    for j, days in ((old, 8), (recent, 1)):
        await conn.execute("update jobs set state='failed', finished_at=datetime('now', ?) || '+00:00' where id=?", (f"-{days} days", j.id))
    await conn.execute("update jobs set finished_at=replace(finished_at, ' ', 'T') where state='failed'"); await conn.commit()
    assert await worker.main(once=True) == 0
    left = {d.name for d in config.staging_dir().iterdir()}
    assert left == {recent.payload["staging_id"]}
    assert (await jobs.get(conn, old.id)).state == "cancelled" and (await jobs.get(conn, recent.id)).state == "failed"


async def test_drain_cleans_a_job_cancelled_while_paused(conn, col, tmp_path, embed_spy):
    tree(tmp_path / "src", {"zbig.txt": BIG}); box = {}

    async def on_call(n):
        if n == 1:
            await jobs.request(conn, box["id"], "pause")
    embed_spy.on_call = on_call
    job = await queue_folder(conn, col, tmp_path / "src"); box["id"] = job.id
    assert await worker.drain(conn) == 1 and (await jobs.get(conn, job.id)).state == "paused"
    await jobs.request(conn, job.id, "cancel")
    assert await worker.drain(conn) == 0
    doc = await kb.find_document(conn, col.id, f"{tmp_path / 'src'}/zbig.txt")
    assert (doc.status, doc.reason) == ("failed", "ingest cancelled") and await get_store().count(kb.vector_name(col.id)) == 0
    assert not sources._root(job.payload["staging_id"]).exists()
    assert (await (await conn.execute("select count(*) from kb_job_items")).fetchone())[0] == 0 and await pipeline.cleanup(conn) == 0
