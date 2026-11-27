import asyncio
import fcntl
import hashlib
import os
import signal
import sqlite3
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


class OtherWriter:
    """Another connection that keeps the database to itself and then commits: a write that got slow."""

    def __init__(self):
        self.conn, self.holds = sqlite3.connect(config.db_path(), isolation_level=None), False

    def hold(self):
        self.conn.execute("begin immediate")
        self.conn.execute("insert or replace into settings(key, value_json) values('slow_write', '1')")
        self.holds = True

    def let_go(self):
        if self.holds:
            self.conn.execute("commit"); self.holds = False


@pytest.fixture
def busy_database(monkeypatch):
    """The worker gives up on a held database after 50 ms and looks again at once; `free_at`
    says in which of its passes the other writer lets go."""
    connect, other, box = db.connect, OtherWriter(), {"passes": 0, "free_at": 0}

    async def quick():
        c = await connect()
        await c.execute("pragma busy_timeout=50")
        return c

    async def one_pass(conn):                     # stands in for the scheduling, which reads as well
        box["passes"] += 1
        if box["passes"] == box["free_at"]:
            other.let_go()
        await jobs.list(conn, limit=1)
    monkeypatch.setattr(db, "connect", quick); monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker.memory, "schedule", one_pass)
    box["other"] = other
    yield box
    other.let_go(); other.conn.close()


async def test_a_busy_database_does_not_end_the_worker(conn, busy_database, monkeypatch, capsys):
    busy_database["free_at"] = 5; ran = []

    async def probe(c, job, *, stop):
        ran.append(job.id)
        await jobs.finish(c, job.id, "done")
        if len(ran) == 1:
            busy_database["other"].hold()             # from here on the worker cannot write
    monkeypatch.setitem(worker.HANDLERS, "probe", probe)
    first, second = await jobs.enqueue(conn, "probe", {}), await jobs.enqueue(conn, "probe", {})
    assert await asyncio.wait_for(worker.main(once=True), 20) == 0
    assert ran == [first.id, second.id] and busy_database["passes"] == 5
    assert [(await jobs.get(conn, j.id)).state for j in (first, second)] == ["done", "done"]
    said = capsys.readouterr().err
    assert said.count("the database is busy") == 1 and said.count("the database answers again") == 1
    assert said.index("the database is busy") < said.index("the database answers again") and "Traceback" not in said


async def test_a_job_the_worker_could_not_close_is_run_again(conn, busy_database, monkeypatch):
    busy_database["free_at"] = 3; attempts = []

    async def probe(c, job, *, stop):
        attempts.append(job.attempts)
        if len(attempts) == 1:
            busy_database["other"].hold()
            raise RuntimeError("boom")                # and the worker cannot write down that it failed
        await jobs.finish(c, job.id, "done")
    monkeypatch.setitem(worker.HANDLERS, "probe", probe)
    job = await jobs.enqueue(conn, "probe", {})
    assert await asyncio.wait_for(worker.main(once=True), 20) == 0
    job = await jobs.get(conn, job.id)
    assert (job.state, job.attempts, job.error, attempts) == ("done", 2, None, [1, 2])


def test_busy_is_sqlites_answer_that_another_connection_holds_the_database(tmp_path):
    a, b = (sqlite3.connect(tmp_path / "x.db", isolation_level=None, timeout=0) for _ in range(2))
    a.execute("pragma journal_mode=wal"); a.execute("create table t(x)")
    b.execute("begin"); b.execute("select * from t").fetchall()         # what b has seen of the database
    a.execute("insert into t values(1)")                                 # and a write that came after it
    with pytest.raises(sqlite3.OperationalError) as stale:
        b.execute("insert into t values(2)")
    b.execute("rollback"); a.execute("begin immediate")
    with pytest.raises(sqlite3.OperationalError) as held:
        b.execute("insert into t values(3)")
    with pytest.raises(sqlite3.OperationalError) as missing:
        b.execute("select * from nowhere")
    assert (stale.value.sqlite_errorname, held.value.sqlite_errorname) == ("SQLITE_BUSY_SNAPSHOT", "SQLITE_BUSY")
    assert worker.busy(stale.value) and worker.busy(held.value) and not worker.busy(missing.value)


async def test_another_database_error_still_ends_the_worker(conn, monkeypatch):
    async def claim(c, kinds):
        raise sqlite3.OperationalError("no such table: jobs")
    monkeypatch.setattr(jobs, "claim", claim)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await asyncio.wait_for(worker.main(once=True), 20)
