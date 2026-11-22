"""How hot the processor and the graphics card are, so that a local model is not loaded onto a
machine that is about to switch itself off. A laptop cuts its power at the processor's critical
temperature (105 C on the owner's) without a word in any log; loading a model of several GB is
the kind of load that takes a hot machine there, and hours of a graphics card at full load heat
the processor that shares its cooling."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

HWMON = Path("/sys/class/hwmon")
SENSORS = ("coretemp", "k10temp", "zenpower")
DEFAULT_LIMIT = 90.0
GPU_DEFAULT_LIMIT = 85.0
CPU_SETTING = "HUNTERAI_MAX_CPU_TEMP"
GPU_SETTING = "HUNTERAI_MAX_GPU_TEMP"
WAIT = 60.0
STEP = 2.0
MARGIN = 5.0                # degrees under its limit a part that was hot has to reach before work that waited goes on
PROCESSOR, GRAPHICS = "processor", "graphics card"      # the parts, as the owner reads them
NVIDIA_SMI = "nvidia-smi"   # the only way to an NVIDIA card's temperature: its driver puts nothing under /sys/class/hwmon
GPU_KEEP = 2.0              # seconds one look at the graphics card serves: embedding looks before every batch
GPU_RETRY = 60.0            # seconds before a program that failed or is not there is tried again
GPU_TIMEOUT = 3.0           # seconds nvidia-smi gets to answer


@dataclass(frozen=True)
class Part:
    """A part of the machine whose temperature has a limit, as it was read just now."""
    name: str               # PROCESSOR or GRAPHICS
    degrees: float
    limit: float
    setting: str            # the variable that sets its limit


def _limit(setting: str, default: float) -> float | None:
    raw = (os.environ.get(setting) or "").strip().lower()
    if raw in ("0", "off"):
        return None
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def limit() -> float | None:
    """The processor temperature at which a local model is not loaded (HUNTERAI_MAX_CPU_TEMP, in C; 0 or off: no limit)."""
    return _limit(CPU_SETTING, DEFAULT_LIMIT)


def gpu_limit() -> float | None:
    """The same for the graphics card (HUNTERAI_MAX_GPU_TEMP, in C; 0 or off: no limit)."""
    return _limit(GPU_SETTING, GPU_DEFAULT_LIMIT)


def cpu_temp() -> float | None:
    """The processor package temperature in C, or None where it cannot be read."""
    try:
        for sensor in sorted(HWMON.iterdir()):
            try:
                if (sensor / "name").read_text().strip() in SENSORS:
                    return int((sensor / "temp1_input").read_text()) / 1000
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


_gpu: tuple[float, float | None] | None = None      # (when the card was last asked, what it said)


def _ask_gpu(program: str) -> float | None:
    """What nvidia-smi says now: the hottest card in C. None when it fails, hangs or names no temperature."""
    try:
        said = subprocess.run([program, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=GPU_TIMEOUT, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    degrees = []
    for line in said.splitlines():
        try:
            degrees.append(float(line.strip()))
        except ValueError:      # "[N/A]": a card that does not say
            continue
    return max(degrees, default=None)


async def gpu_temp() -> float | None:
    """The temperature of the graphics card in C (of the hottest, where there are several), or None where it
    cannot be read: no NVIDIA card, no nvidia-smi, or a driver that does not answer. Asking starts a program, so
    one answer serves for GPU_KEEP seconds, and a failure for GPU_RETRY."""
    global _gpu
    if _gpu is not None and time.monotonic() - _gpu[0] < (GPU_RETRY if _gpu[1] is None else GPU_KEEP):
        return _gpu[1]
    program = shutil.which(NVIDIA_SMI)
    degrees = await asyncio.to_thread(_ask_gpu, program) if program else None
    _gpu = (time.monotonic(), degrees)
    return degrees


async def parts() -> list[Part]:
    """The parts that have a limit and can be read now: the processor, then the graphics card."""
    out = []
    top = limit()
    now = cpu_temp() if top is not None else None
    if now is not None:
        out.append(Part(PROCESSOR, now, top, CPU_SETTING))
    top = gpu_limit()
    now = await gpu_temp() if top is not None else None
    if now is not None:
        out.append(Part(GRAPHICS, now, top, GPU_SETTING))
    return out


async def hot() -> Part | None:
    """The part that is at or over its limit, or None. A part that cannot be read holds nothing up."""
    return next((p for p in await parts() if p.degrees >= p.limit), None)


async def wait_cool(wait: float = WAIT, step: float = STEP, margin: float = 0.0,
                    tell: Callable[[Part], Awaitable[None]] | None = None) -> Part | None:
    """None when no part is at or over its limit, or as soon as every part that was has dropped `margin`
    degrees under it. Waits up to `wait` seconds for that and then returns the part that is still hot. `tell`
    is given the hot part before every pause; what it raises ends the wait."""
    was_hot: set[str] = set()
    until = time.monotonic() + wait
    while True:
        now = await parts()
        was_hot.update(p.name for p in now if p.degrees >= p.limit)
        still = next((p for p in now if p.name in was_hot and (p.degrees >= p.limit or p.degrees > p.limit - margin)), None)
        if still is None:
            return None
        if tell is not None:
            await tell(still)
        if time.monotonic() >= until:
            return still
        await asyncio.sleep(step)
