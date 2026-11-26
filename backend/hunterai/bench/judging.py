"""Stage two of a bench: after it ended, a judge model reads the answers whose mark hangs on their wording.

The model under test and the judge are never asked in turns: a bench ends, and then its stored runs are read, one after
another, by one judge. So one model at a time holds the memory of this machine. One judging runs at a time, and none
while a bench runs. What the judge says is kept beside the marks (repo.bench) and changes none of them.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .. import cooling, problems
from ..providers import registry
from ..repo import bench as store
from ..repo.bench import BenchResult, Judging, ModelRef
from . import cases, judge, run, runner
from .cases import Case, CaseError

ERRORS_IN_A_ROW = 3     # answers in a row the judge could not be asked about: then the judging ends
GONE = "The set of this bench cannot be read any more, and a judge needs its tests to know what a right answer is"
NOTHING = "No run of this bench has an answer whose mark hangs on its wording, in a test that says what a right answer is."
STILL = "This bench is still running. A judge reads it when it has ended."
TOO_HOT = "The judging was stopped before the judge {model} was asked. Let the machine cool and judge the bench again."


class Busy(Exception):
    """A judging or a bench is running, and there is one at a time."""


@dataclass
class _State:
    run_id: str
    progress: dict
    task: asyncio.Task | None = None
    started: bool = False       # the task has run its first line: before that, cancelling it would skip its end
    cancel: bool = False
    stopping: bool = False
    finalizing: bool = False


_now: _State | None = None
_starting: str | None = None    # the bench whose judging is on its way: asked for, and not at its first answer yet


def running() -> str | None:
    """The id of the bench that is being judged or is about to be, if one is. A bench that begins, or the delete of
    this one, in the moment between the request and the first answer would be beside a judge otherwise."""
    return _now.run_id if _now is not None else _starting


def progress(run_id: str) -> dict | None:
    """How far the judging of this bench is: the answers read of how many, and `doing`: "running", or "cooling" while
    it waits for the machine. None when it is not being judged."""
    return dict(_now.progress) if _now is not None and _now.run_id == run_id else None


def _to_read(results: list[BenchResult], by_id: dict[str, Case]) -> list[tuple[BenchResult, Case]]:
    return [(r, by_id[r.case_id]) for r in results
            if r.case_id in by_id and by_id[r.case_id].judge and judge.by_wording(r.status, r.reasons)]


async def start(conn_factory, run_id: str, ref: ModelRef) -> Judging:
    """Starts the judging of a bench that has ended and returns it as it was stored. Busy while a bench or a judging
    runs; KeyError for a bench that is not there; ValueError, with the reason, for the rest."""
    global _now, _starting
    if _now is not None or _starting is not None or runner.busy():
        if runner.running() == run_id:
            raise ValueError(STILL)
        raise Busy
    _starting = run_id
    try:
        try:
            registry.get(ref.provider)
        except KeyError:
            raise ValueError(f"unknown provider {ref.provider}") from None
        conn = await conn_factory()
        try:
            bench = await store.get(conn, run_id)
            if bench is None:
                raise KeyError(run_id)
            try:
                set_ = await asyncio.to_thread(cases.load, bench.set_id)
            except (KeyError, CaseError) as e:
                raise ValueError(f"{GONE}: {e.args[0] if e.args else e}") from None
            todo = _to_read(await store.results(conn, run_id), {c.id: c for c in set_.cases})
            if not todo:
                raise ValueError(NOTHING)
            stored = await store.start_judging(conn, run_id, ref, len(todo))
        finally:
            await conn.close()
        state = _State(run_id, {"done": 0, "total": len(todo), "doing": "running"})
        state.task = asyncio.create_task(_run(conn_factory, state, ref, todo))
        _now = state
        return stored
    finally:
        _starting = None


async def cancel(run_id: str) -> bool:
    """Ends the judging of this bench, if it is the one that runs. What it read stays."""
    state = _now
    if state is None or state.run_id != run_id or state.finalizing or state.task is None or state.task.done():
        return False
    state.cancel = True
    if state.started:
        state.task.cancel()
    return True


async def wait(run_id: str) -> None:
    """Returns when this bench is no longer being judged (at once when it is not)."""
    state = _now
    if state is not None and state.run_id == run_id and state.task is not None:
        await asyncio.gather(state.task, return_exceptions=True)


async def shutdown() -> None:
    """HunterAI stops: the judging that runs ends as interrupted, and is waited for."""
    state = _now
    if state is None or state.task is None:
        return
    state.stopping = True
    await cancel(state.run_id)
    await asyncio.gather(state.task, return_exceptions=True)


async def recover(conn) -> None:
    """At HunterAI's start: a judging the stop before cut off is marked as interrupted."""
    await store.interrupt_judgings(conn)


async def _run(conn_factory, state: _State, ref: ModelRef, todo: list[tuple[BenchResult, Case]]) -> None:
    state.started = True
    status, note, conn = "stopped", "", None
    try:
        conn = await conn_factory()
        if state.cancel:
            raise asyncio.CancelledError
        here, failed = cooling.here(ref.provider, ref.model), 0

        async def tell(doing: str) -> None:
            state.progress["doing"] = doing

        for result, case in todo:
            if here:
                await run._cool(ref.model, tell, TOO_HOT)       # a judge that runs on this machine heats it like any model
            said = await judge.ask(ref.provider, ref.model, case, result.calls, result.answer)
            await store.add_judgment(conn, result.id, said.verdict, said.reason, said.error)
            state.progress["done"] += 1
            failed = failed + 1 if said.verdict is None and said.error != judge.NO_VERDICT else 0
            if failed >= ERRORS_IN_A_ROW:
                note = f"The judge could not be asked three times in a row: {said.error}"
                break
        else:
            status = "done"
    except cooling.TooHot as e:
        note = str(e)
    except asyncio.CancelledError:
        status, note = ("interrupted", store.JUDGING_INTERRUPTED) if state.stopping else ("cancelled", "")
    except Exception as e:  # noqa: BLE001 - whatever it is, the judging ends with a word about it
        note = problems.own_failure(e, "judging a bench")
    finally:
        state.finalizing = True
        await asyncio.shield(_end(conn_factory, conn, state, status, note))


async def _end(conn_factory, conn, state: _State, status: str, note: str) -> None:
    global _now
    try:
        if conn is None:
            conn = await conn_factory()
        await store.finish_judging(conn, state.run_id, status, note)
    except Exception as e:  # noqa: BLE001
        problems.own_failure(e, "writing the end of a judging")
    finally:
        try:
            if conn is not None:
                await conn.close()
        finally:
            if _now is state:
                _now = None
