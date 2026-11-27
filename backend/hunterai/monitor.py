"""The machine as it is now, for the Live monitor: how busy the processor is, who holds the memory, and what each
graphics card does. Read from /proc and from nvidia-smi. What cannot be read is None, and a machine without an NVIDIA
card has no cards. Nothing here runs unless the page asks for it: a look at the cards starts two programs, and
keeps a card awake that a laptop would put to sleep."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import thermal

PROC = Path("/proc")
HOLDERS = 4                 # programs that are named for the memory they hold; the rest is one sum
CPU_LOOK = 0.2              # seconds between the two looks at the processor's counters when there is no earlier look
CPU_STALE = 15.0            # seconds after which an earlier look is too old to measure against
KEEP = 2.0                  # seconds one answer of nvidia-smi serves
RETRY = 60.0                # seconds before a program that failed or is not there is tried again
TIMEOUT = 3.0               # seconds nvidia-smi gets to answer
MIB = 1024 ** 2
QUERY = "index,utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total,name"      # the name last: it may hold a comma
_PYTHON = re.compile(r"python[0-9.]*")
_TAKES_A_VALUE = ("-W", "-X")       # options of Python whose next argument is theirs, not the script

_cpu: tuple[float, int, int] | None = None              # (when, ticks of work, all ticks) at the last look
_seen: tuple[float, list[dict] | None] | None = None    # (when the cards were last asked, what they said)


# ---- the processor

def _ticks() -> tuple[int, int] | None:
    """(ticks of work, all ticks) of all processors since the machine started, from the first line of /proc/stat.
    Time spent idle or waiting for a disk is not work."""
    try:
        first = (PROC / "stat").read_text().splitlines()[0].split()
        if first[0] != "cpu":
            return None
        user, nice, system, idle, iowait, irq, softirq, steal = (int(n) for n in first[1:9])
    except (OSError, ValueError, IndexError):
        return None
    work = user + nice + system + irq + softirq + steal
    return work, work + idle + iowait


async def _moment() -> None:
    await asyncio.sleep(CPU_LOOK)


async def cpu_load() -> float | None:
    """The share of the processors' time that was work since the last look, in percent. Without a look that is
    recent enough it takes two, a moment apart. None where the counters cannot be read, and when no time passed."""
    global _cpu
    now = _ticks()
    if now is None:
        return None
    before = _cpu[1:] if _cpu is not None and time.monotonic() - _cpu[0] <= CPU_STALE else None
    if before is None:
        before = now
        await _moment()
        now = _ticks()
        if now is None:
            return None
    _cpu = (time.monotonic(), *now)
    passed = now[1] - before[1]
    return round(100 * (now[0] - before[0]) / passed, 1) if passed > 0 else None


# ---- the memory

def _runs(of: Path) -> str | None:
    """What a Python process was started to run: the package after -m, or the script without its folder and ".py"."""
    try:
        args = iter((of / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")[1:])
    except OSError:
        return None
    for arg in args:
        if arg == "-m":
            return next(args, "").split(".")[0] or None
        if arg == "-c":
            return None
        if arg in _TAKES_A_VALUE:
            next(args, None)
        elif arg and not arg.startswith("-"):
            return Path(arg).name.removesuffix(".py") or None
    return None


def _called(of: Path, otherwise: str) -> str:
    """What the process in this folder of /proc is called: the name of the file it runs, and for Python what it was
    started to run. `otherwise` where the file cannot be read, as for a process of another user."""
    try:
        name = Path(os.readlink(of / "exe").removesuffix(" (deleted)")).name
    except OSError:
        return otherwise
    return (_runs(of) if _PYTHON.fullmatch(name) else None) or name or otherwise


def _largest(held: dict[str, int]) -> list[dict]:
    return [{"name": name, "bytes": size} for name, size in sorted(held.items(), key=lambda h: (-h[1], h[0]))[:HOLDERS]]


def _holding() -> dict[str, int]:
    """Bytes of memory each program holds for itself (RssAnon of its processes, added up by what they are called).
    Files a process has mapped are left out: the kernel can give that memory to another, and counts it as available."""
    held: dict[str, int] = {}
    try:
        folders = [f for f in PROC.iterdir() if f.name.isdigit()]
    except OSError:
        return held
    for of in folders:
        try:
            said = (of / "status").read_text()
        except OSError:                 # it ended while the list was read
            continue
        name = size = None
        for line in said.splitlines():
            if line.startswith("Name:"):
                name = line[5:].strip()
            elif line.startswith("RssAnon:"):
                size = line.split()[1]
                break
        if not (name and size and size.isdigit() and int(size)):        # a thread of the kernel has no such line
            continue
        called = _called(of, name)
        held[called] = held.get(called, 0) + int(size) * 1024
    return held


def memory() -> dict | None:
    """Memory in use and all of it, in bytes, with the programs that hold the most of it, largest first. In use is
    what the kernel does not call available. None where /proc/meminfo does not say."""
    seen = {}
    try:
        for line in (PROC / "meminfo").read_text().splitlines():
            name, _, rest = line.partition(":")
            if name in ("MemTotal", "MemAvailable"):
                seen[name] = int(rest.split()[0]) * 1024
        total, available = seen["MemTotal"], seen["MemAvailable"]
    except (OSError, ValueError, KeyError, IndexError):
        return None
    return {"used": total - available, "total": total, "holders": _largest(_holding())}


# ---- the graphics cards

def _said(program: str, *args: str) -> str | None:
    try:
        return subprocess.run([program, *args], capture_output=True, text=True, timeout=TIMEOUT, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:                  # "[N/A]": the card does not say
        return None


def _listed(program: str) -> list[dict]:
    """Each card nvidia-smi lists, with its index. None of them when it fails or hangs."""
    said = _said(program, f"--query-gpu={QUERY}", "--format=csv,noheader,nounits")
    cards = []
    for line in (said or "").splitlines():
        parts = [p.strip() for p in line.split(",", 6)]
        if len(parts) != 7:
            continue
        index, load, temp, power, used, total, name = parts
        used, total = _number(used), _number(total)
        cards.append({"index": index, "name": name, "load": _number(load), "temp": _number(temp), "power": _number(power),
                      "used": None if used is None else int(used * MIB), "total": None if total is None else int(total * MIB)})
    return cards


def _held(program: str) -> dict[str, dict[str, int]]:
    """Who holds each card's memory, from the table of `nvidia-smi pmon`: for the index of a card, bytes by the name
    of the program. Its heading names the columns, which differ between drivers. A process whose memory the driver
    does not tell is left out."""
    held: dict[str, dict[str, int]] = {}
    columns: list[str] = []
    for line in (_said(program, "pmon", "-c", "1", "-s", "m") or "").splitlines():
        if line.startswith("#"):
            words = line[1:].split()
            if "pid" in words and "fb" in words:
                columns = words
            continue
        row = dict(zip(columns, line.split(None, len(columns) - 1))) if columns else {}
        pid, size = row.get("pid", ""), row.get("fb", "")
        if not (pid.isdigit() and size.isdigit()):
            continue
        name = _called(PROC / pid, row.get("command", "").strip()) or pid
        of = held.setdefault(row.get("gpu", "0"), {})
        of[name] = of.get(name, 0) + int(size) * MIB
    return held


async def cards() -> list[dict]:
    """The NVIDIA cards as they are now: name, load in percent, temperature with its limit, power in watts, memory
    in use and all of it in bytes, and the programs that hold the most of it. One look serves for KEEP seconds, and a
    failure for RETRY."""
    global _seen
    if _seen is not None and time.monotonic() - _seen[0] < (RETRY if _seen[1] is None else KEEP):
        return _seen[1] or []
    program = shutil.which(thermal.NVIDIA_SMI)
    found = None
    if program:
        listed, held = await asyncio.gather(asyncio.to_thread(_listed, program), asyncio.to_thread(_held, program))
        if listed:
            limit = thermal.gpu_limit()
            found = [{"name": c["name"], "load": c["load"], "temp": c["temp"], "limit": limit, "power": c["power"],
                      "used": c["used"], "total": c["total"], "holders": _largest(held.get(c["index"], {}))} for c in listed]
    _seen = (time.monotonic(), found)
    return found or []


async def read() -> dict:
    """One reading for the Live monitor."""
    load, held, seen = await asyncio.gather(cpu_load(), asyncio.to_thread(memory), cards())
    return {"cpu": {"load": load, "temp": thermal.cpu_temp(), "limit": thermal.limit()}, "ram": held, "cards": seen}
