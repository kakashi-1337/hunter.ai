"""Run an external program for ingest: argv only, a timeout, a growth limit, and the whole
process group killed before any failure is reported."""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
from dataclasses import dataclass
from pathlib import Path

MAX_STDOUT = 16 * 1024**2


@dataclass
class ToolResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ToolMissing(Exception):
    pass


class ToolTimeout(Exception):
    pass


class ToolLimit(Exception):
    pass


def _tree_bytes(root: Path) -> int:
    total = 0
    for dirpath, _, names in os.walk(root):
        for n in names:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
            except OSError:
                pass
    return total


async def _drain(stream, limit: int | None) -> bytes:
    buf = bytearray()
    while True:
        part = await stream.read(65536)
        if not part:
            return bytes(buf)
        buf += part
        if limit is not None and len(buf) > limit:
            raise ToolLimit(f"more than {limit} bytes of output")
        if limit is None and len(buf) > 1024**2:
            del buf[:-65536]      # keep the tail of stderr only


def _die_with_parent() -> None:
    """In the child, before it becomes the program: ask the kernel to kill it when the process
    that started it is gone. A worker that is killed (a deploy, a crash) would otherwise leave
    a clone or a converter running, writing into a folder the next worker starts over in."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)        # PR_SET_PDEATHSIG; Linux only
    except Exception:  # noqa: BLE001 - another system: the program runs as before
        pass


async def run_tool(argv: list[str], *, timeout: float, cwd: Path, grow_dir: Path | None = None,
                   grow_limit: int = 0, env: dict[str, str] | None = None) -> ToolResult:
    exe = shutil.which(argv[0])
    if exe is None:
        raise ToolMissing(argv[0])
    proc = await asyncio.create_subprocess_exec(
        exe, *argv[1:], cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True, env=env, preexec_fn=_die_with_parent)

    async def watch_growth():
        while True:
            await asyncio.sleep(0.25)
            if await asyncio.to_thread(_tree_bytes, grow_dir) > grow_limit:
                raise ToolLimit(f"wrote more than {grow_limit} bytes")

    async def work() -> ToolResult:
        out = asyncio.ensure_future(_drain(proc.stdout, MAX_STDOUT))
        err = asyncio.ensure_future(_drain(proc.stderr, None))
        tasks = [out, err, asyncio.ensure_future(proc.wait())]
        guard = asyncio.ensure_future(watch_growth()) if grow_dir is not None else None
        try:
            pending = set(tasks) | ({guard} if guard else set())
            while not all(t.done() for t in tasks):
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    t.result()     # raises ToolLimit from a drain or from the growth guard
            return ToolResult(proc.returncode, out.result(), err.result())
        finally:
            for t in (*tasks, guard):
                if t is not None and not t.done():
                    t.cancel()

    try:
        return await asyncio.wait_for(work(), timeout)
    except asyncio.TimeoutError:
        raise ToolTimeout(f"{argv[0]} timed out after {timeout:g} s") from None
    finally:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.shield(proc.wait())
        else:
            try:                    # the leader is gone; anything it left in its group goes too
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
