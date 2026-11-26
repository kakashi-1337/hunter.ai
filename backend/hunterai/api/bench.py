"""The bench over HTTP: the sets, the start of a bench, how far it is, its table, cancel and delete."""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .. import db
from ..bench import cases, runner
from ..bench.cases import CaseError
from ..repo import bench as store
from ..repo.bench import BenchRun, ModelRef
from .deps import get_conn, not_found

router = APIRouter(prefix="/api/bench")
BUSY = "A bench is running. Wait for it or cancel it."
RUNNING = "This bench is running. Cancel it first."
NOT_JUDGED = "This run has no mark to set right: its model call failed, or it was skipped."


class ModelIn(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=300)


class StartIn(BaseModel):
    models: list[ModelIn]                  # how many: the runner says (runner.MODELS_MAX)
    set_id: str = Field(max_length=200)
    repeats: int = 3


class MarkIn(BaseModel):
    status: Literal["pass", "fail"] | None      # the owner's mark; null takes it away again. It has to be sent


def _seen(run: BenchRun) -> BenchRun:
    """A bench as it is shown. One that the table still calls running while nothing runs it could not write its
    end (a full disk, say): it is over, and it is shown as cut off."""
    if run.status == "running" and runner.running() != run.id:
        return run.model_copy(update={"status": "interrupted"})
    return run


async def _bench(conn, run_id: str) -> BenchRun:
    run = await store.get(conn, run_id)
    if run is None:
        raise not_found("bench")
    return run


@router.get("/sets")
async def list_sets():
    return await asyncio.to_thread(cases.available)        # files are read and checked: not on the server's own thread


@router.post("/runs")
async def start(body: StartIn):
    refs = [ModelRef(provider=m.provider, model=m.model) for m in body.models]
    try:
        return await runner.start(db.connect, refs, body.set_id, body.repeats)
    except runner.Busy:
        raise HTTPException(409, BUSY) from None
    except KeyError:
        raise not_found("set") from None
    except CaseError as e:
        raise HTTPException(422, f"This set cannot be used: {e}") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/runs")
async def list_runs(conn=Depends(get_conn)):
    return [{"run": _seen(run), "progress": runner.progress(run.id)} for run in await store.list(conn)]


@router.get("/runs/{rid}")
async def get_run(rid: str, conn=Depends(get_conn)):
    run = await _bench(conn, rid)
    return {"run": _seen(run), "marks": await store.marks(conn, rid), "progress": runner.progress(rid)}


@router.get("/runs/{rid}/results")
async def get_cell(rid: str, provider: str, model: str, case_id: str, conn=Depends(get_conn)):
    """The runs of one case on one model, with their lines, calls and answers."""
    await _bench(conn, rid)
    return await store.cell(conn, rid, ModelRef(provider=provider, model=model), case_id)


@router.put("/results/{result_id}/mark")
async def set_mark(result_id: str, body: MarkIn, conn=Depends(get_conn)):
    """The owner's own mark for one run, where the rules of the set misread its answer. The mark of the rules stays."""
    try:
        result = await store.set_mark(conn, result_id, body.status)
    except store.NotJudged:
        raise HTTPException(409, NOT_JUDGED) from None
    if result is None:
        raise not_found("result")
    return result


@router.post("/runs/{rid}/cancel", status_code=204)
async def cancel(rid: str, conn=Depends(get_conn)):
    await _bench(conn, rid)
    await runner.cancel(rid)        # a bench that is over has nothing to cancel, and that is fine
    return Response(status_code=204)


@router.delete("/runs/{rid}", status_code=204)
async def delete(rid: str, conn=Depends(get_conn)):
    await _bench(conn, rid)
    if runner.running() == rid:
        raise HTTPException(409, RUNNING)
    await store.delete(conn, rid)
    return Response(status_code=204)
