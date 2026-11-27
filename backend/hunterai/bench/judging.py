"""Stage two of a bench, and the check of the judge that does it.

After a bench ended, a judge model reads the answers whose mark hangs on their wording (`start`). The model under
test and the judge are never asked in turns: a bench ends, and then its stored runs are read, one after another, by
one judge. What the judge says is kept beside the marks (repo.bench) and changes none of them.

Before a judge is trusted with that, it can be asked about the answers of a set whose right verdict is known
(`start_check`), and what it said is counted. That is the same loop: one judge, one answer after another.

One of the two runs at a time, and none while a bench runs. So one model at a time holds the memory of this machine.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .. import cooling, problems
from ..db import new_id
from ..providers import registry
from ..repo import bench as store
from ..repo.bench import BenchResult, CallBrief, JudgeCheck, Judging, ModelRef
from . import cases, judge, run, runner
from .cases import Case, CaseError, Known

ERRORS_IN_A_ROW = 3     # answers in a row the judge could not be asked about: then it ends
GONE = "The set of this bench cannot be read any more, and a judge needs its tests to know what a right answer is"
NOTHING = "No run of this bench has an answer whose mark hangs on its wording, in a test that says what a right answer is."
NO_KNOWN = "This set has no answers whose right verdict is known. A test gets them as [[case.known]]."
STILL = "This bench is still running. A judge reads it when it has ended."
TOO_HOT = "The judging was stopped before the judge {model} was asked. Let the machine cool and judge the bench again."
CHECK_TOO_HOT = "The check was stopped before the judge {model} was asked. Let the machine cool and check the judge again."


class Busy(Exception):
    """A bench, a judging or a check of a judge is running, and there is one at a time."""


@dataclass(frozen=True)
class _Kind:
    """What the two things a judge does differ in, beside where its verdicts are kept."""
    name: str
    what: str               # in a word about a failure of HunterAI's own
    interrupted: str        # the note of one that a stop of HunterAI ended
    too_hot: str            # the sentence about what was stopped when the machine stays hot


JUDGING = _Kind("judging", "judging a bench", store.JUDGING_INTERRUPTED, TOO_HOT)
CHECK = _Kind("check", "checking a judge", store.CHECK_INTERRUPTED, CHECK_TOO_HOT)


@dataclass
class _Reading:
    """One answer the judge is asked about, and where what it says of it is kept."""
    case: Case
    calls: list[CallBrief]
    answer: str
    keep: Callable[[Any, judge.Judgment], Awaitable[None]]


@dataclass
class _State:
    kind: _Kind
    key: str                    # the id of the bench that is judged, or of the check
    progress: dict
    finish: Callable[[Any, str, str], Awaitable[None]]      # writes how it ended: connection, status, note
    task: asyncio.Task | None = None
    started: bool = False       # the task has run its first line: before that, cancelling it would skip its end
    cancel: bool = False
    stopping: bool = False
    finalizing: bool = False


_now: _State | None = None
# What is on its way: asked for, and not at its first answer yet. Its kind and its key.
_starting: tuple[_Kind, str] | None = None


def busy() -> bool:
    """Whether a judge model is at work or about to be: it reads a bench, or it is being checked."""
    return _now is not None or _starting is not None


def running() -> str | None:
    """The id of the bench that is being judged or is about to be, if one is. A bench that begins, or the delete of
    this one, in the moment between the request and the first answer would be beside a judge otherwise."""
    if _now is not None:
        return _now.key if _now.kind is JUDGING else None
    return _starting[1] if _starting is not None and _starting[0] is JUDGING else None


def checking() -> str | None:
    """The id of the check of a judge that runs or is about to, if one does."""
    if _now is not None:
        return _now.key if _now.kind is CHECK else None
    return _starting[1] if _starting is not None and _starting[0] is CHECK else None


def _progress(kind: _Kind, key: str) -> dict | None:
    return dict(_now.progress) if _now is not None and _now.kind is kind and _now.key == key else None


def progress(run_id: str) -> dict | None:
    """How far the judging of this bench is: the answers read of how many, and `doing`: "running", or "cooling" while
    it waits for the machine. None when it is not being judged."""
    return _progress(JUDGING, run_id)


def check_progress(check_id: str) -> dict | None:
    """The same for a check of a judge: the known answers asked about of how many."""
    return _progress(CHECK, check_id)


def _to_read(results: list[BenchResult], by_id: dict[str, Case]) -> list[tuple[BenchResult, Case]]:
    return [(r, by_id[r.case_id]) for r in results
            if r.case_id in by_id and by_id[r.case_id].judge and judge.by_wording(r.status, r.reasons)]


def _provider(ref: ModelRef) -> None:
    try:
        registry.get(ref.provider)
    except KeyError:
        raise ValueError(f"unknown provider {ref.provider}") from None


def _beside_the_mark(result_id: str) -> Callable[[Any, judge.Judgment], Awaitable[None]]:
    async def keep(conn, said: judge.Judgment) -> None:
        await store.add_judgment(conn, result_id, said.verdict, said.reason, said.error)
    return keep


def _in_the_check(check_id: str, case_id: str, n: int, known: Known) -> Callable[[Any, judge.Judgment], Awaitable[None]]:
    async def keep(conn, said: judge.Judgment) -> None:
        await store.add_checked(conn, check_id, case_id, n, known.verdict, known.answer, said.verdict, said.reason, said.error)
    return keep


def _launch(conn_factory, state: _State, ref: ModelRef, readings: list[_Reading]) -> None:
    global _now
    state.task = asyncio.create_task(_run(conn_factory, state, ref, readings))
    _now = state


async def start(conn_factory, run_id: str, ref: ModelRef) -> Judging:
    """Starts the judging of a bench that has ended and returns it as it was stored. Busy while a bench, a judging or
    a check runs; KeyError for a bench that is not there; ValueError, with the reason, for the rest."""
    global _starting
    if busy() or runner.busy():
        if runner.running() == run_id:
            raise ValueError(STILL)
        raise Busy
    _starting = (JUDGING, run_id)
    try:
        _provider(ref)
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

        async def finish(conn, status: str, note: str) -> None:
            await store.finish_judging(conn, run_id, status, note)

        readings = [_Reading(case, result.calls, result.answer, _beside_the_mark(result.id)) for result, case in todo]
        _launch(conn_factory, _State(JUDGING, run_id, {"done": 0, "total": len(todo), "doing": "running"}, finish), ref, readings)
        return stored
    finally:
        _starting = None


async def start_check(conn_factory, set_id: str, ref: ModelRef) -> JudgeCheck:
    """Starts a check of this judge on the known answers of this set and returns it as it was stored. Busy while a
    bench, a judging or a check runs; KeyError for a set that is not there, CaseError for one that cannot be used;
    ValueError, with the reason, for the rest."""
    global _starting
    if busy() or runner.busy():
        raise Busy
    check_id = new_id()
    _starting = (CHECK, check_id)
    try:
        _provider(ref)
        set_ = await asyncio.to_thread(cases.load, set_id)      # a file is read and checked: not on the server's own thread
        known = [(case, n, answer) for case in set_.cases for n, answer in enumerate(case.known, 1)]
        if not known:
            raise ValueError(NO_KNOWN)
        conn = await conn_factory()
        try:
            stored = await store.start_check(conn, set_, ref, len(known), check_id)
        finally:
            await conn.close()

        async def finish(conn, status: str, note: str) -> None:
            await store.finish_check(conn, check_id, status, note)

        readings = [_Reading(case, [CallBrief(name=call.name, arguments=call.arguments) for call in answer.calls], answer.answer,
                             _in_the_check(check_id, case.id, n, answer)) for case, n, answer in known]
        _launch(conn_factory, _State(CHECK, check_id, {"done": 0, "total": len(known), "doing": "running"}, finish), ref, readings)
        return stored
    finally:
        _starting = None


async def _cancel(kind: _Kind, key: str) -> bool:
    state = _now
    if (state is None or state.kind is not kind or state.key != key or state.finalizing or state.task is None
            or state.task.done()):
        return False
    state.cancel = True
    if state.started:
        state.task.cancel()
    return True


async def cancel(run_id: str) -> bool:
    """Ends the judging of this bench, if it is the one that runs. What it read stays."""
    return await _cancel(JUDGING, run_id)


async def cancel_check(check_id: str) -> bool:
    """Ends this check of a judge, if it is the one that runs. What it asked stays."""
    return await _cancel(CHECK, check_id)


async def _wait(kind: _Kind, key: str) -> None:
    state = _now
    if state is not None and state.kind is kind and state.key == key and state.task is not None:
        await asyncio.gather(state.task, return_exceptions=True)


async def wait(run_id: str) -> None:
    """Returns when this bench is no longer being judged (at once when it is not)."""
    await _wait(JUDGING, run_id)


async def wait_check(check_id: str) -> None:
    """Returns when this check no longer runs (at once when it does not)."""
    await _wait(CHECK, check_id)


async def shutdown() -> None:
    """HunterAI stops: the judging or the check that runs ends as interrupted, and is waited for."""
    state = _now
    if state is None or state.task is None:
        return
    state.stopping = True
    await _cancel(state.kind, state.key)
    await asyncio.gather(state.task, return_exceptions=True)


async def recover(conn) -> None:
    """At HunterAI's start: a judging or a check the stop before cut off is marked as interrupted."""
    await store.interrupt_judgings(conn)
    await store.interrupt_checks(conn)


async def _run(conn_factory, state: _State, ref: ModelRef, readings: list[_Reading]) -> None:
    state.started = True
    status, note, conn = "stopped", "", None
    try:
        conn = await conn_factory()
        if state.cancel:
            raise asyncio.CancelledError
        here, failed = cooling.here(ref.provider, ref.model), 0

        async def tell(doing: str) -> None:
            state.progress["doing"] = doing

        for reading in readings:
            if here:
                await run._cool(ref.model, tell, state.kind.too_hot)        # a judge that runs on this machine heats it like any model
            said = await judge.ask(ref.provider, ref.model, reading.case, reading.calls, reading.answer)
            await reading.keep(conn, said)
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
        status, note = ("interrupted", state.kind.interrupted) if state.stopping else ("cancelled", "")
    except Exception as e:  # noqa: BLE001 - whatever it is, it ends with a word about it
        note = problems.own_failure(e, state.kind.what)
    finally:
        state.finalizing = True
        await asyncio.shield(_end(conn_factory, conn, state, status, note))


async def _end(conn_factory, conn, state: _State, status: str, note: str) -> None:
    global _now
    try:
        if conn is None:
            conn = await conn_factory()
        await state.finish(conn, status, note)
    except Exception as e:  # noqa: BLE001
        problems.own_failure(e, "writing the end of " + ("a judging" if state.kind is JUDGING else "a check of a judge"))
    finally:
        try:
            if conn is not None:
                await conn.close()
        finally:
            if _now is state:
                _now = None
