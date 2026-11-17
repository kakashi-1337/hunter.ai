"""Processes the execute tool starts on the host.

Every command runs as `bash -c` in its own session, writes to a log file (never a pipe, so
nothing a child leaves behind can block a read), and is ended by killing its whole process
group. A process's lifetime is its leader's: when bash exits, whatever is left of the
group is killed.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal

from pydantic import BaseModel

from . import config, db
from .repo import runs
from .tools.base import ToolError

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800
KILL_GRACE = 3.0
KILL_WAIT = 10.0            # after SIGKILL, how long kill() waits for the reaper
REAP_INTERVAL = 0.2
MAX_LOG_BYTES = 64 * 1024**2
FREE_DISK_FLOOR = 10 * 1024**3
MAX_PROCS_PER_CHAT = 8
READ_BYTES = 24_000
FOREGROUND_BYTES = 1024**2
BACKGROUND_FIRST_WAIT = 2.0

# Never handed to a child: API keys and HunterAI's own variables. HUNTERAI_PRIVATE_ENV adds
# prefixes of your own (comma-separated, for example "MYTOOL_,INTERNAL_") for variables of other
# tools that live in the same environment.
ENV_DROP = re.compile(r"(_API_KEY$|^HUNTERAI_|^ABLIT_)")


def _private(name: str) -> bool:
    extra = tuple(p.strip() for p in (os.environ.get("HUNTERAI_PRIVATE_ENV") or "").split(",") if p.strip())
    return bool(ENV_DROP.search(name)) or (bool(extra) and name.startswith(extra))


def child_env() -> dict[str, str]:
    from . import toolbox
    env = {k: v for k, v in os.environ.items() if not _private(k)}
    env["PATH"] = toolbox.path_with_folder(env.get("PATH") or "/usr/local/bin:/usr/bin:/bin")   # the owner's tool folder first
    return env


class Proc(BaseModel):
    id: str
    chat_id: str
    run_id: str | None
    step_id: str | None
    pid: int | None
    pgid: int | None
    command: str
    cwd: str
    log_path: str
    background: bool
    status: str            # running | exited | killed | lost
    exit_code: int | None = None
    started_at: str
    ended_at: str | None = None


class _Handle:
    def __init__(self, proc: Proc, process: asyncio.subprocess.Process):
        self.proc, self.process = proc, process
        self.done = asyncio.Event()
        self.killed = False
        self.reaper: asyncio.Task | None = None


_live: dict[str, _Handle] = {}


def _row(r) -> Proc:
    return Proc(id=r["id"], chat_id=r["chat_id"], run_id=r["run_id"], step_id=r["step_id"],
                pid=r["pid"], pgid=r["pgid"], command=r["command"], cwd=r["cwd"],
                log_path=r["log_path"], background=bool(r["background"]), status=r["status"],
                exit_code=r["exit_code"], started_at=r["started_at"], ended_at=r["ended_at"])


def _ticks(pid: int) -> int | None:
    """The process's start time (field 22 of /proc/<pid>/stat): tells a pid from its reuse."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _group_gone(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False


def _free() -> int:
    return shutil.disk_usage(config.proc_dir()).free


async def start(*, chat_id: str, run_id: str, step_id: str, command: str, cwd: str,
                background: bool, env: dict[str, str] | None = None) -> Proc:
    """`env` is added to what every command is given (`child_env`), and wins where both name a variable."""
    if _free() < FREE_DISK_FLOOR:
        raise ToolError(f"not enough free disk for process output: {_free() / 1024**3:.1f} GiB "
                        "free, the floor is 10 GiB")
    pid_ = db.new_id()
    log = config.proc_dir() / f"{pid_}.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        process = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command, cwd=cwd, env={**child_env(), **(env or {})},
            stdin=asyncio.subprocess.PIPE if background else asyncio.subprocess.DEVNULL,
            stdout=fd, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    finally:
        os.close(fd)
    proc = Proc(id=pid_, chat_id=chat_id, run_id=run_id, step_id=step_id, pid=process.pid,
                pgid=process.pid, command=command, cwd=cwd, log_path=str(log),
                background=background, status="running", started_at=db.now())
    handle = _Handle(proc, process)
    _live[pid_] = handle
    # The reaper exists before anything else can fail or be cancelled: a process without
    # one would have no timeout, and kill() and shutdown() would wait for it for ever.
    handle.reaper = asyncio.create_task(_reap(handle))
    try:
        conn = await db.connect()
        try:
            await conn.execute(
                "insert into processes(id,chat_id,run_id,step_id,pid,pgid,start_ticks,command,"
                "cwd,log_path,background,status,started_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proc.id, chat_id, run_id, step_id, proc.pid, proc.pgid, _ticks(process.pid),
                 command, cwd, proc.log_path, int(background), "running", proc.started_at))
            await conn.commit()
        finally:
            await conn.close()
    except BaseException:
        # Not recorded (cancelled, or the database is unavailable): it must not keep running.
        handle.killed = True
        _signal_group(proc.pgid, signal.SIGKILL)
        raise
    return proc


def _append(log_path: str, text: str) -> None:
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


async def _reap(h: _Handle) -> None:
    """Watch one process until it ends, enforce its caps, and record how it ended."""
    proc, marker = h.proc, None
    waiter = asyncio.ensure_future(h.process.wait())
    try:
        while True:
            done, _ = await asyncio.wait({waiter}, timeout=REAP_INTERVAL)
            if done:
                break
            if marker is None:
                try:
                    size = os.path.getsize(proc.log_path)
                except OSError:
                    size = 0
                if size > MAX_LOG_BYTES:
                    marker = f"\n[killed: output passed {MAX_LOG_BYTES // 1024**2} MiB]\n"
                elif _free() < FREE_DISK_FLOOR:
                    marker = "\n[killed: free disk below 10 GiB]\n"
                if marker:
                    h.killed = True
                    _signal_group(proc.pgid, signal.SIGKILL)
        code = h.process.returncode
    finally:
        waiter.cancel()
        # The leader is gone: nothing of its group outlives it.
        _signal_group(proc.pgid, signal.SIGKILL)
    for _ in range(250):
        if _group_gone(proc.pgid):
            break
        await asyncio.sleep(0.02)
    if marker:
        # A fast writer can pass the cap between two checks; what stays on disk is the cap.
        try:
            if os.path.getsize(proc.log_path) > MAX_LOG_BYTES:
                os.truncate(proc.log_path, MAX_LOG_BYTES)
        except OSError:
            pass
        _append(proc.log_path, marker)
    stdin = h.process.stdin
    if stdin is not None:
        try:
            stdin.close()
            await stdin.wait_closed()
        except Exception:
            pass
    proc.status = "killed" if h.killed else "exited"
    proc.exit_code, proc.ended_at = code, db.now()
    try:
        conn = await db.connect()
        try:
            await conn.execute("update processes set status=?, exit_code=?, ended_at=? where id=?",
                               (proc.status, code, proc.ended_at, proc.id))
            await conn.commit()
            if proc.background and proc.step_id:
                # The run is usually over by now, so nothing is published; the UI refetches.
                await runs.patch_step(
                    conn, proc.step_id,
                    status="done" if code == 0 and not h.killed else "error",
                    detail={"exit_code": code, "process_status": proc.status}, close=True)
        finally:
            await conn.close()
    except Exception:
        pass   # the row is closed by recover() at the next start
    finally:
        h.done.set()


def _handle(proc_id: str) -> _Handle:
    h = _live.get(proc_id)
    if h is None:
        raise ToolError("no such process")
    return h


async def kill(proc_id: str) -> Proc:
    """SIGTERM to the group, a grace period, SIGKILL to the group, then wait until it is gone."""
    h = _handle(proc_id)
    if not h.done.is_set():
        h.killed = True
        _signal_group(h.proc.pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(h.done.wait()), KILL_GRACE)
        except asyncio.TimeoutError:
            _signal_group(h.proc.pgid, signal.SIGKILL)
            try:   # bounded: nothing may make a kill, and with it the app's stop, wait for ever
                await asyncio.wait_for(asyncio.shield(h.done.wait()), KILL_WAIT)
            except asyncio.TimeoutError:
                pass
    return h.proc


async def wait(proc_id: str, timeout: float) -> Proc:
    """The final state when the process ends; on timeout or cancellation it is killed."""
    h = _handle(proc_id)
    try:
        await asyncio.wait_for(asyncio.shield(h.done.wait()), timeout)
    except asyncio.TimeoutError:
        await kill(proc_id)
    except asyncio.CancelledError:
        await asyncio.shield(kill(proc_id))
        raise
    return h.proc


def read(log_path: str, offset: int = 0, limit: int = READ_BYTES) -> tuple[str, int, int]:
    """(text, next offset, total size) of a process log; works for any app instance's log."""
    try:
        total = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            f.seek(offset)
            data = f.read(limit)
    except OSError:
        return "", offset, 0
    return data.decode("utf-8", errors="replace"), offset + len(data), total


async def send_input(proc_id: str, text: str, eof: bool = False) -> None:
    h = _handle(proc_id)
    if h.done.is_set() or not h.proc.background or h.process.stdin is None:
        raise ToolError("the process is not running in the background")
    try:
        h.process.stdin.write(text.encode("utf-8"))
        await h.process.stdin.drain()
        if eof:
            h.process.stdin.close()
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        raise ToolError("the process no longer reads input") from None


async def get(proc_id: str) -> Proc | None:
    h = _live.get(proc_id)
    if h is not None:
        return h.proc
    conn = await db.connect()
    try:
        r = await (await conn.execute("select * from processes where id=?", (proc_id,))).fetchone()
        return _row(r) if r else None
    finally:
        await conn.close()


async def list_for_chat(conn, chat_id: str) -> list[Proc]:
    rows = await (await conn.execute(
        "select * from processes where chat_id=? order by started_at desc, rowid desc",
        (chat_id,))).fetchall()
    return [(_live[r["id"]].proc if r["id"] in _live else _row(r)) for r in rows]


def running(chat_id: str | None = None) -> list[Proc]:
    return [h.proc for h in _live.values() if not h.done.is_set()
            and (chat_id is None or h.proc.chat_id == chat_id)]


async def kill_chat(chat_id: str) -> int:
    targets = [p.id for p in running(chat_id)]
    await asyncio.gather(*(kill(i) for i in targets))
    return len(targets)


async def shutdown() -> None:
    """Kill every running process and wait until its row and its step are final."""
    await asyncio.gather(*(kill(p.id) for p in running()), return_exceptions=True)
    reapers = [h.reaper for h in _live.values() if h.reaper is not None]
    if reapers:
        await asyncio.gather(*reapers, return_exceptions=True)


async def recover(conn) -> None:
    """At startup: a process a previous instance left running is killed (when it is still the
    same process) or marked lost."""
    rows = await (await conn.execute(
        "select * from processes where status='running'")).fetchall()
    for r in rows:
        if r["id"] in _live:
            continue
        same = r["pid"] and r["start_ticks"] is not None and _ticks(r["pid"]) == r["start_ticks"]
        if same and r["pgid"] and r["pgid"] > 1:
            _signal_group(r["pgid"], signal.SIGKILL)
        await conn.execute("update processes set status=?, ended_at=? where id=?",
                           ("killed" if same else "lost", db.now(), r["id"]))
    await conn.commit()


def kill_all_now() -> None:
    """Synchronous last resort (tests, interpreter exit): SIGKILL every group still running."""
    for h in _live.values():
        if not h.done.is_set():
            _signal_group(h.proc.pgid, signal.SIGKILL)


def reset() -> None:
    """Forget the in-memory handles without killing anything (tests)."""
    _live.clear()
