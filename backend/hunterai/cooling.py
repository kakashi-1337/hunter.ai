"""Before a model is loaded onto this machine, a processor or a graphics card that is at its limit is given
time to cool: loading reads several GB with the processor at full power, and a laptop that reaches its critical
temperature under it switches itself off in the middle of the work (see thermal.py). The wait is a step of the
run, so the owner sees what the run is waiting for.

A model that is in memory already is called at once, however hot the machine is. The heat is then the model's
own: the owner's laptop is at 96 to 98 C all the while its local model answers, 86 C on the graphics card. A wait
before each of those calls cools nothing for long, makes every step of a run slow, and stops the run when the
machine does not get five degrees under a limit it is over in ordinary work."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from . import hub, thermal
from .providers import colibri, ollama, registry
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


class _InMemory(Exception):
    """The model is in memory: the call loads nothing, so there is nothing to wait for."""


async def loads(provider_name: str, model: str) -> bool:
    """Whether a call to this model makes the machine load it. True when nobody says what is in memory: an Ollama
    that does not answer the question, a colibri server that is not there."""
    if provider_name == colibri.NAME:       # a colibri server holds its model from its start to its end
        return (await registry.get(colibri.NAME).served()).state != "ok"
    return await ollama.in_memory(model) is not True


async def wait_to_load(provider_name: str, model: str, wait: float, step: float, margin: float = 0.0,
                       tell: Callable[[thermal.Part], Awaitable[None]] | None = None, *, reload: bool = False
                       ) -> thermal.Part | None:
    """thermal.wait_cool for a call to a model on this machine: it waits only when the call loads the model. What
    is in memory is asked once, and only when a part is hot: a cool machine is asked nothing. `reload`: the call
    loads the model whatever is in memory (it names another context window than the one the model was loaded with)."""
    asked = False

    async def first(part: thermal.Part) -> None:
        nonlocal asked
        if not asked:
            asked = True
            if not reload and not await loads(provider_name, model):
                raise _InMemory
        if tell is not None:
            await tell(part)
    try:
        return await thermal.wait_cool(wait, step, margin, first)
    except _InMemory:
        return None


def _stopped(part: thermal.Part, model: str, reload: bool = False) -> str:
    why = ("it has to be loaded again for a larger context window" if reload
           else "it is not in memory as far as can be told")
    return (f"Stopped: the {part.name} is at {part.degrees:.0f} °C and did not cool to {part.limit - thermal.MARGIN:.0f} °C "
            f"within {WAIT / 60:.0f} minutes (its limit is {part.limit:.0f} °C). The local model {model} was not called: "
            f"{why}, and loading a model onto a machine this hot can switch the machine "
            f"off. Let it cool and send the message again. {part.setting} sets the limit in °C (off: no limit).")


async def before_call(conn, run_id: str, provider_name: str, model: str, parent_step: str | None = None, *,
                      reload: bool = False) -> None:
    """Returns when the machine is cool enough for a call to this model, which is at once for a model that runs
    elsewhere, for one that is in memory, and for a machine that is not at a limit. A hot machine that has to load
    the model gets up to WAIT seconds to drop a few degrees under the limit, shown as a "cooling" step of the run;
    TooHot when it has not by then. `reload`: the call loads the model though it is in memory, for a context window
    of another size."""
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
        still = await wait_to_load(provider_name, model, WAIT, STEP, thermal.MARGIN, tell, reload=reload)
        status = "done" if still is None else "error"
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        if step is not None:
            done = await asyncio.shield(runs.finish_step(conn, step.id, status))
            await asyncio.shield(hub.publish(run_id, {"type": "step_finished", "step": done.model_dump()}))
    if still is not None:
        raise TooHot(_stopped(still, model, reload))
