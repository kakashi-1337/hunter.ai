"""The worker process: runs queued jobs one at a time, outside the web server, so a long
ingest neither blocks a chat nor dies with a restart of the app.

    uv run python -m hunterai.worker [--once]
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import sys
from collections.abc import Callable
from datetime import datetime, timezone

from . import config, db, memory
from .kb import pipeline, sources
from .repo import jobs, settings

HANDLERS: dict[str, Callable] = {"ingest": pipeline.run_job, "reembed": pipeline.run_reembed,
                                 "memory": memory.run_job}
POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 2.0
ALIVE_SECONDS = 10.0
FAILED_KEEP_SECONDS = 7 * 86400


def _age(stamp: str | None) -> float:
    if not stamp:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except ValueError:
        return float("inf")


async def alive(conn) -> bool:
    return _age(await settings.get(conn, "worker_heartbeat")) < ALIVE_SECONDS


async def drain(conn, *, stop: Callable[[], bool] = lambda: False) -> int:
    """Run queued jobs until none is left or `stop()` is true. Cleans up on every pass."""
    ran = 0
    await pipeline.cleanup(conn)
    while not stop():
        job = await jobs.claim(conn, list(HANDLERS))
        if job is None:
            break
        try:
            await HANDLERS[job.kind](conn, job, stop=stop)
        except Exception as e:  # noqa: BLE001 - a handler bug fails its job, not the worker
            await jobs.finish(conn, job.id, "failed", f"{type(e).__name__}: {e}"[:300])
        ran += 1
        await pipeline.cleanup(conn)
    return ran


async def _startup(conn) -> None:
    await jobs.requeue_running(conn, list(HANDLERS))          # the crash resume
    for job in await jobs.list(conn, kind="ingest", states=["failed"], limit=None):
        if _age(job.finished_at) > FAILED_KEEP_SECONDS:        # a forgotten failure holds staging
            await jobs.request(conn, job.id, "cancel")
    await pipeline.cleanup(conn)
    keep = {j.payload.get("staging_id") for j in await jobs.list(
        conn, kind="ingest", states=["queued", "running", "paused", "failed"], limit=None)}
    sources.sweep_staging(keep={k for k in keep if k})


async def main(once: bool = False) -> int:
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    lock = os.open(home / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        print("another HunterAI worker is running", file=sys.stderr)
        return 3
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    handled = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopping.set)
            handled.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    conn = beat_conn = beat = None
    try:
        conn = await db.connect()
        await db.migrate(conn)
        await _startup(conn)
        beat_conn = await db.connect()

        async def heartbeat():
            while True:
                try:
                    await settings.put(beat_conn, "worker_heartbeat", db.now())
                except Exception:  # noqa: BLE001 - a busy database must not stop the heartbeat for good
                    pass
                await asyncio.sleep(HEARTBEAT_SECONDS)
        beat = asyncio.create_task(heartbeat())
        while not stopping.is_set():
            try:
                await memory.schedule(conn)        # chats that went quiet get their notes taken
            except Exception:  # noqa: BLE001 - scheduling must never stop the worker
                pass
            await drain(conn, stop=stopping.is_set)
            if once and not await jobs.list(conn, states=["queued"], limit=1):
                break
            try:
                await asyncio.wait_for(stopping.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
        return 0
    finally:
        if beat is not None:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        for c in (beat_conn, conn):
            if c is not None:
                await c.close()
        for sig in handled:
            loop.remove_signal_handler(sig)
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(once="--once" in sys.argv[1:])))
