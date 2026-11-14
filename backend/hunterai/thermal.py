"""How hot the processor is, so that a local model is not loaded onto a machine that is about
to switch itself off. A laptop cuts its power at the processor's critical temperature (105 C
on the owner's) without a word in any log; loading a model of several GB is the kind of load
that takes a hot machine there."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

HWMON = Path("/sys/class/hwmon")
SENSORS = ("coretemp", "k10temp", "zenpower")
DEFAULT_LIMIT = 90.0
WAIT = 60.0
STEP = 2.0


def limit() -> float | None:
    """The temperature above which a local model is not loaded (HUNTERAI_MAX_CPU_TEMP, in C; 0 or off: no limit)."""
    raw = (os.environ.get("HUNTERAI_MAX_CPU_TEMP") or "").strip().lower()
    if raw in ("0", "off"):
        return None
    try:
        return float(raw) if raw else DEFAULT_LIMIT
    except ValueError:
        return DEFAULT_LIMIT


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


async def cool_enough(wait: float = WAIT, step: float = STEP) -> tuple[bool, float | None]:
    """(whether the processor is at or under the limit, its temperature). Waits up to `wait`
    seconds for a hot processor to cool. A machine whose temperature cannot be read passes."""
    top = limit()
    until = time.monotonic() + wait
    while True:
        now = cpu_temp()
        if top is None or now is None or now <= top:
            return True, now
        if time.monotonic() >= until:
            return False, now
        await asyncio.sleep(step)
