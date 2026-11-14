"""In-memory fan-out of run events to any number of SSE subscribers.

Every event of a run is buffered, so a subscriber that connects late (or reconnects
after a page reload) replays the run from its first event.
"""
from __future__ import annotations

import asyncio

KEEP_SECONDS = 60
_runs: dict[str, "_Run"] = {}


class _Run:
    def __init__(self):
        self.events: list[dict] = []
        self.finished = False
        self.changed = asyncio.Condition()


def open(run_id: str) -> None:  # noqa: A001 - hub API name
    _runs.setdefault(run_id, _Run())


def known(run_id: str) -> bool:
    return run_id in _runs


async def publish(run_id: str, event: dict) -> None:
    run = _runs.setdefault(run_id, _Run())
    async with run.changed:
        run.events.append(event)
        if event.get("type") == "run_finished":
            run.finished = True
        run.changed.notify_all()
    if run.finished:
        try:
            asyncio.get_running_loop().call_later(KEEP_SECONDS, _runs.pop, run_id, None)
        except RuntimeError:
            _runs.pop(run_id, None)


async def subscribe(run_id: str):
    """Yield every event of the run from the start; ends after run_finished."""
    run = _runs.get(run_id)
    if run is None:
        return
    i = 0
    while True:
        async with run.changed:
            while i >= len(run.events) and not run.finished:
                await run.changed.wait()
            batch = run.events[i:]
            done = run.finished
        for e in batch:
            yield e
        i += len(batch)
        if done and i >= len(run.events):
            return


def reset() -> None:
    _runs.clear()
