"""A whole bench: every case of a set on every chosen model, one run after another.

One bench runs at a time. It is one task of the server, like the run of a chat, and it stores each result as it
comes: what was done is kept when the bench is cancelled, when the machine is too hot to go on, and when
HunterAI stops.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .. import cooling, problems
from ..providers import registry
from ..repo import bench as store
from ..repo.bench import BenchRun, ModelRef
from ..tools import registry as tools
from . import cases, score
from .cases import CaseSet
from .run import run_case

REPEATS_MAX = 5
MODELS_MAX = 20
ERRORS_IN_A_ROW = 3     # runs of one model in a row that end with an error or at the time limit: then its rest is skipped
SKIPPED = "skipped: three runs of this model in a row ended with an error or at the time limit"
NAMED = 10              # tools the note about unknown tools names one by one


class Busy(Exception):
    """A bench is running, and there is one at a time."""


@dataclass
class _State:
    """The bench that is running."""
    run_id: str
    progress: dict
    note: str                           # what the bench said at its start; the reason it ended early comes after it
    task: asyncio.Task | None = None
    started: bool = False               # the task has run its first line: before that, cancelling it would skip its end
    cancel: bool = False                # the owner asked for the end
    stopping: bool = False              # HunterAI is being stopped
    finalizing: bool = False            # past the point where a cancel means anything


_now: _State | None = None
_starting = False                       # a start is on its way: a second one at the same moment is busy


def running() -> str | None:
    """The id of the bench that is running, if one is."""
    return _now.run_id if _now is not None else None


def busy() -> bool:
    """Whether a bench runs or one is on its way: from the request to start one, before it has an id."""
    return _now is not None or _starting


def progress(run_id: str) -> dict | None:
    """Where the bench is: the model, case and repeat it is at (counted from 1) of how many, the runs done of the
    total, and `doing`: "running", or "cooling" while it waits for the machine. None when that bench is not running."""
    return dict(_now.progress) if _now is not None and _now.run_id == run_id else None


def _unknown_tools(set_: CaseSet) -> str:
    """A word about tools the set names that HunterAI does not have now: a typo in a hand-written set, or an MCP
    server that is not connected. The reader of the sets cannot know the tools; a bench can."""
    named = {name for case in set_.cases
             for name in (*case.expect.calls_any, *case.expect.calls_none, *(n for entry in case.tools for n in entry.name))}
    unknown = sorted(name for name in named if tools.get(name) is None)
    if not unknown:
        return ""
    listed = ", ".join(cases.shown(name)[1:-1] for name in unknown[:NAMED])
    if len(unknown) > NAMED:
        listed += f" and {len(unknown) - NAMED} more"
    return (f"This set names tools HunterAI does not have at the moment: {listed}. "
            "A test that needs one of them cannot pass.")


async def start(conn_factory, models: list[ModelRef], set_id: str, repeats: int, thinking_off: bool = False) -> BenchRun:
    """Starts a bench and returns it as it was stored. Busy when one runs; KeyError for a set that is not there,
    CaseError for one that cannot be used; ValueError, with the reason, for the rest. `thinking_off`: every call of
    it is asked without thinking."""
    global _now, _starting
    picked = list(dict.fromkeys((m.provider, m.model) for m in models))      # a model picked twice runs once
    if not picked:
        raise ValueError("pick at least one model")
    if len(picked) > MODELS_MAX:
        raise ValueError(f"pick at most {MODELS_MAX} models")
    if not 1 <= repeats <= REPEATS_MAX:
        raise ValueError(f"repeats is 1 to {REPEATS_MAX}")
    for provider, _ in picked:
        try:
            registry.get(provider)
        except KeyError:
            raise ValueError(f"unknown provider {provider}") from None
    if _now is not None or _starting:
        raise Busy
    _starting = True
    try:
        set_ = await asyncio.to_thread(cases.load, set_id)      # a file is read and checked: not on the server's own thread
        refs = [ModelRef(provider=provider, model=model) for provider, model in picked]
        note = _unknown_tools(set_)
        conn = await conn_factory()
        try:
            bench = await store.create(conn, set_, refs, repeats, note, thinking_off)
        finally:
            await conn.close()
        state = _State(bench.id, {"model": 1, "models": len(refs), "case": 1, "cases": len(set_.cases), "repeat": 1,
                                  "repeats": repeats, "done": 0, "total": len(refs) * len(set_.cases) * repeats,
                                  "doing": "running"}, note)
        state.task = asyncio.create_task(_run(conn_factory, state, bench, set_))
        _now = state
        return bench
    finally:
        _starting = False


async def cancel(run_id: str) -> bool:
    """Ends the bench that is running, if it is this one. What it stored stays."""
    state = _now
    if state is None or state.run_id != run_id or state.finalizing or state.task is None or state.task.done():
        return False
    state.cancel = True
    if state.started:           # a task cancelled before its first line never runs its body, and nothing would end the bench
        state.task.cancel()
    return True


async def wait(run_id: str) -> None:
    """Returns when this bench is no longer running (at once when it is not)."""
    state = _now
    if state is not None and state.run_id == run_id and state.task is not None:
        await asyncio.gather(state.task, return_exceptions=True)


async def shutdown() -> None:
    """HunterAI stops: the bench that is running ends as interrupted, and is waited for."""
    state = _now
    if state is None or state.task is None:
        return
    state.stopping = True
    await cancel(state.run_id)
    await asyncio.gather(state.task, return_exceptions=True)


async def recover(conn) -> None:
    """At HunterAI's start: a bench the stop before cut off is marked as interrupted."""
    await store.interrupt_running(conn)


async def _open(conn_factory):
    """(a connection, whether the task was cancelled while it opened). The opening runs to its end either way: a
    connection that is half open would keep its thread."""
    opening = asyncio.ensure_future(conn_factory())
    try:
        return await asyncio.shield(opening), False
    except asyncio.CancelledError:
        return await opening, True


async def _run(conn_factory, state: _State, bench: BenchRun, set_: CaseSet) -> None:
    state.started = True
    status, reason, conn = "stopped", "", None
    try:
        conn, cancelled = await _open(conn_factory)
        if cancelled or state.cancel:
            raise asyncio.CancelledError
        await _all(conn, state, bench, set_)
        status = "done"
    except cooling.TooHot as e:
        reason = str(e)
    except asyncio.CancelledError:
        status, reason = ("interrupted", store.INTERRUPTED) if state.stopping else ("cancelled", "")
    except Exception as e:  # noqa: BLE001 - whatever it is, the bench ends with a word about it
        reason = problems.own_failure(e, "running the bench")
    finally:
        state.finalizing = True
        await asyncio.shield(_end(conn_factory, conn, state, status, reason))


async def _end(conn_factory, conn, state: _State, status: str, reason: str) -> None:
    """Writes how the bench ended and makes room for the next one. When that cannot be written, the bench stays
    marked as running until HunterAI's next start marks it interrupted; the next one can begin all the same."""
    global _now
    try:
        if conn is None:
            conn = await conn_factory()
        await store.finish(conn, state.run_id, status, " ".join(part for part in (state.note, reason) if part))
    except Exception as e:  # noqa: BLE001
        problems.own_failure(e, "writing the end of a bench")       # into the error log
    finally:
        try:
            if conn is not None:
                await conn.close()
        finally:
            if _now is state:
                _now = None


async def _all(conn, state: _State, bench: BenchRun, set_: CaseSet) -> None:
    """Every case on every model: all runs of one model before the next (a local model is loaded once)."""
    async def tell(doing: str) -> None:
        state.progress["doing"] = doing

    most = 0                                # tools a model of this bench was offered
    for m, ref in enumerate(bench.models, 1):
        failed, window = 0, None            # runs in a row that did not end by themselves; the window this model was raised to
        for c, case in enumerate(set_.cases, 1):
            for repeat in range(1, bench.repeats + 1):
                state.progress.update(model=m, case=c, repeat=repeat, doing="running")
                if failed >= ERRORS_IN_A_ROW:
                    await store.add_result(conn, bench.id, ref, case.id, repeat, "skipped", [SKIPPED])
                    state.progress["done"] += 1
                    continue
                began, ran, t = time.monotonic(), None, None
                try:
                    ran = await run_case(conn, ref.provider, ref.model, case, tell, window, bench.thinking_off)
                    t, window = ran.transcript, ran.window
                    verdict = score.score(case, t)
                except (cooling.TooHot, asyncio.CancelledError):
                    raise
                except Exception as e:  # noqa: BLE001 - one run that breaks is the error of that run, not the end of hours of them
                    verdict = score.Verdict(status="error", reasons=[problems.own_failure(e, "running this test")])
                # The time of a run is the model's: a pause for a hot machine would make the model after it look slow.
                took = ran.seconds if ran is not None else time.monotonic() - began
                said = verdict.reasons + ([score.ASKED_AGAIN] if t is not None and t.asked_again else [])
                await store.add_result(conn, bench.id, ref, case.id, repeat, verdict.status, said, t, int(took * 1000))
                if ran is not None and ran.tools_offered > most:
                    most = ran.tools_offered
                    await store.note_tools(conn, bench.id, most)
                failed = failed + 1 if verdict.status == "error" or (t is not None and t.timed_out) else 0
                state.progress["done"] += 1
