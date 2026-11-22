"""Before a model on this machine is called, a processor or a graphics card that is at its limit is given
time to cool. An agent run is one model call after another for as long as the task takes: nothing else in it
lets a hot laptop cool, and one that reaches its critical temperature switches itself off in the middle of the
work (see thermal.py). The wait is a step of the run, so the owner sees what the run is waiting for."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from . import hub, thermal
from .providers import colibri, ollama
from .repo import runs

WAIT = 180.0        # seconds a run waits for the machine to cool, then it stops
STEP = 3.0          # seconds between two looks at the temperature
THIS_MACHINE = frozenset(("127.0.0.1", "localhost", "::1"))


class TooHot(Exception):
    """The machine did not cool in time. The message is for the owner."""


def _on_this_machine(address: str | None) -> bool:
    try:
        return (urlsplit(address or "").hostname or "") in THIS_MACHINE
    except ValueError:
        return False


def here(provider_name: str, model: str) -> bool:
    """Whether a call to this model heats this machine: a model of an Ollama or a colibri that listens on this
    machine. Not one on another machine, and not one that Ollama runs in its own cloud (their tag ends in
    "cloud": gpt-oss:120b-cloud, glm-4.6:cloud)."""
    if provider_name == ollama.OllamaProvider.name:
        tag = model.rpartition(":")[2].lower() if ":" in model else ""
        return _on_this_machine(ollama.host()) and not (tag == "cloud" or tag.endswith("-cloud"))
    return provider_name == colibri.NAME and _on_this_machine(colibri.address())


def _stopped(part: thermal.Part) -> str:
    return (f"Stopped: the {part.name} is at {part.degrees:.0f} °C and did not cool to {part.limit - thermal.MARGIN:.0f} °C "
            f"within {WAIT / 60:.0f} minutes (its limit is {part.limit:.0f} °C). The local model was not called: a machine "
            f"this hot can switch itself off under the load. Let it cool and send the message again. {part.setting} sets "
            "the limit in °C (off: no limit).")


async def before_call(conn, run_id: str, provider_name: str, model: str, parent_step: str | None = None) -> None:
    """Returns when the machine is cool enough for a call to this model, which is at once for a model that runs
    elsewhere and for a machine that is not at a limit. A hot one gets up to WAIT seconds to drop a few degrees
    under the limit, shown as a "cooling" step of the run; TooHot when it has not by then."""
    if not here(provider_name, model):
        return
    step = None

    async def tell(part: thermal.Part) -> None:
        nonlocal step
        if step is None:
            step = await runs.add_step(conn, run_id, "cooling", f"{part.name} at {part.degrees:.0f} °C, limit {part.limit:.0f} °C",
                                       {"part": part.name, "degrees": part.degrees, "limit": part.limit}, parent_step)
            await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})

    status, still = "error", None
    try:
        still = await thermal.wait_cool(WAIT, STEP, thermal.MARGIN, tell)
        status = "done" if still is None else "error"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        if step is not None:
            done = await asyncio.shield(runs.finish_step(conn, step.id, status))
            await asyncio.shield(hub.publish(run_id, {"type": "step_finished", "step": done.model_dump()}))
    if still is not None:
        raise TooHot(_stopped(still))
