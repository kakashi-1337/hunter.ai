"""The benches that were run, and their results (hunterai/bench/)."""
from __future__ import annotations

import builtins
import json
from typing import Literal

from pydantic import BaseModel

from ..bench.cases import CaseBrief, CaseSet, briefs
from ..bench.score import Transcript
from ..db import new_id, now

ANSWER_CHARS = 8_000     # of an answer that are kept
ARGS_CHARS = 500         # of the arguments of one call, as JSON text
PROBLEM_CHARS = 300      # of what a refused call was told
CALLS_KEPT = 50          # calls of one run; how many were made is kept beside them
REASONS_KEPT = 40        # lines of one mark
INTERRUPTED = "HunterAI stopped while this bench was running."


class ModelRef(BaseModel):
    provider: str
    model: str


class CallBrief(BaseModel):
    name: str
    arguments: str                  # the start of the arguments as JSON text
    problem: str | None = None      # the start of what the model was told, when a chat would not have run the call


class BenchRun(BaseModel):
    id: str
    set_id: str
    set_name: str
    repeats: int
    models: list[ModelRef]
    cases: list[CaseBrief]
    status: Literal["running", "done", "cancelled", "interrupted", "stopped"]
    note: str
    tools_offered: int
    created_at: str
    ended_at: str | None


class BenchResult(BaseModel):
    id: str
    run_id: str
    provider: str
    model: str
    case_id: str
    repeat: int
    status: Literal["pass", "fail", "error", "skipped"]
    reasons: list[str]
    calls: list[CallBrief]
    calls_made: int
    answer: str
    turns: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    created_at: str


def _cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[:most] + "…"


def _run(r) -> BenchRun:
    return BenchRun(id=r["id"], set_id=r["set_id"], set_name=r["set_name"], repeats=r["repeats"],
                    models=json.loads(r["models_json"]), cases=json.loads(r["cases_json"]), status=r["status"],
                    note=r["note"], tools_offered=r["tools_offered"], created_at=r["created_at"], ended_at=r["ended_at"])


def _result(r) -> BenchResult:
    return BenchResult(id=r["id"], run_id=r["run_id"], provider=r["provider"], model=r["model"], case_id=r["case_id"],
                       repeat=r["repeat"], status=r["status"], reasons=json.loads(r["reasons_json"]),
                       calls=json.loads(r["calls_json"]), calls_made=r["calls_made"], answer=r["answer"], turns=r["turns"],
                       input_tokens=r["input_tokens"], output_tokens=r["output_tokens"], duration_ms=r["duration_ms"],
                       created_at=r["created_at"])


async def create(conn, set_: CaseSet, models: builtins.list[ModelRef], repeats: int, note: str = "") -> BenchRun:
    """A bench that is running. The cases are kept as they are now: the set's file can change afterwards."""
    rid = new_id()
    await conn.execute(
        "insert into bench_runs(id,set_id,set_name,repeats,models_json,cases_json,status,note,created_at)"
        " values(?,?,?,?,?,?,?,?,?)",
        (rid, set_.id, set_.name, repeats, json.dumps([m.model_dump() for m in models]),
         json.dumps([c.model_dump() for c in briefs(set_)]), "running", note, now()))
    await conn.commit()
    return await get(conn, rid)


async def add_result(conn, run_id: str, ref: ModelRef, case_id: str, repeat: int, status: str, reasons: builtins.list[str],
                     t: Transcript | None = None, duration_ms: int = 0) -> BenchResult:
    """The mark of one run of a case with what the run left behind, or without (`t` is None) for a run that was
    never made. Long things are kept as their start."""
    calls = t.calls if t is not None else []
    kept = [CallBrief(name=c.name, arguments=_cut(json.dumps(c.arguments, ensure_ascii=False, default=str), ARGS_CHARS),
                      problem=None if c.problem is None else _cut(c.problem, PROBLEM_CHARS)).model_dump()
            for c in calls[:CALLS_KEPT]]
    more = len(reasons) - REASONS_KEPT
    lines = reasons if more <= 0 else [*reasons[:REASONS_KEPT], f"and {more} more line{'' if more == 1 else 's'}"]
    rid = new_id()
    await conn.execute(
        "insert into bench_results(id,run_id,provider,model,case_id,repeat,status,reasons_json,calls_json,calls_made,answer,"
        "turns,input_tokens,output_tokens,duration_ms,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, run_id, ref.provider, ref.model, case_id, repeat, status, json.dumps(lines), json.dumps(kept), len(calls),
         _cut(t.answer, ANSWER_CHARS) if t is not None else "", t.turns if t is not None else 0,
         t.input_tokens if t is not None else 0, t.output_tokens if t is not None else 0, duration_ms, now()))
    await conn.commit()
    return _result(await (await conn.execute("select * from bench_results where id=?", (rid,))).fetchone())


async def note_tools(conn, run_id: str, offered: int) -> None:
    await conn.execute("update bench_runs set tools_offered=? where id=?", (offered, run_id))
    await conn.commit()


async def finish(conn, run_id: str, status: str, note: str = "") -> BenchRun:
    await conn.execute("update bench_runs set status=?, note=?, ended_at=? where id=?", (status, note, now(), run_id))
    await conn.commit()
    return await get(conn, run_id)


async def get(conn, run_id: str) -> BenchRun | None:
    r = await (await conn.execute("select * from bench_runs where id=?", (run_id,))).fetchone()
    return _run(r) if r else None


async def results(conn, run_id: str) -> builtins.list[BenchResult]:
    """The results of a bench in the order they were added."""
    rows = await (await conn.execute("select * from bench_results where run_id=? order by rowid", (run_id,))).fetchall()
    return [_result(r) for r in rows]


async def list(conn) -> builtins.list[BenchRun]:
    """Every bench, the newest first."""
    rows = await (await conn.execute("select * from bench_runs order by created_at desc, rowid desc")).fetchall()
    return [_run(r) for r in rows]


async def delete(conn, run_id: str) -> bool:
    cur = await conn.execute("delete from bench_runs where id=?", (run_id,))
    await conn.commit()
    return cur.rowcount > 0


async def interrupt_running(conn) -> int:
    """At HunterAI's start: a bench that is still marked as running was cut off by the stop before. How many."""
    cur = await conn.execute("update bench_runs set status='interrupted', note=?, ended_at=? where status='running'",
                             (INTERRUPTED, now()))
    await conn.commit()
    return cur.rowcount
