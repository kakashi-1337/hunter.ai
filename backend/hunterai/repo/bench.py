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
THINKING_CHARS = 4_000   # of the thinking of the last call of a run: its end, which is where the model was when it stopped
ARGS_CHARS = 500         # of the arguments of one call, as JSON text
PROBLEM_CHARS = 300      # of what a refused call was told
CALLS_KEPT = 50          # calls of one run; how many were made is kept beside them
REASONS_KEPT = 40        # lines of one mark
INTERRUPTED = "HunterAI stopped while this bench was running."
# A bench with what it was started with beside it, when that was more than its models, set and repeats
RUNS = "select b.*, o.options_json from bench_runs b left join bench_options o on o.run_id = b.id"
# A result with the mark its owner set by hand beside it, when he set one
RESULTS = ("select r.*, c.status as by_hand, k.text as thinking from bench_results r"
           " left join bench_corrections c on c.result_id = r.id left join bench_thinking k on k.result_id = r.id")


class NotJudged(Exception):
    """The run has no mark of the rules that could be set right: its model call failed, or it was never made."""


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
    thinking_off: bool = False      # every call of it is asked without thinking; else as the model has it
    created_at: str
    ended_at: str | None


class BenchResult(BaseModel):
    id: str
    run_id: str
    provider: str
    model: str
    case_id: str
    repeat: int
    status: Literal["pass", "fail", "error", "skipped"]        # what the rules of the set gave
    by_hand: Literal["pass", "fail"] | None = None             # what the owner says it is, where he set the mark right
    reasons: list[str]
    calls: list[CallBrief]
    calls_made: int
    answer: str
    thinking: str = ""              # the end of what the model thought in its last call, when it thought
    turns: int
    input_tokens: int
    output_tokens: int
    duration_ms: int
    created_at: str


class Mark(BaseModel):
    """A result as the table of a bench needs it: without its lines, calls and answer."""
    provider: str
    model: str
    case_id: str
    repeat: int
    status: Literal["pass", "fail", "error", "skipped"]
    by_hand: Literal["pass", "fail"] | None = None
    turns: int
    input_tokens: int
    output_tokens: int
    duration_ms: int


def _cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[:most] + "…"


def _run(r) -> BenchRun:
    options = json.loads(r["options_json"] or "{}")
    return BenchRun(id=r["id"], set_id=r["set_id"], set_name=r["set_name"], repeats=r["repeats"],
                    models=json.loads(r["models_json"]), cases=json.loads(r["cases_json"]), status=r["status"],
                    note=r["note"], tools_offered=r["tools_offered"], thinking_off=options.get("thinking_off") is True,
                    created_at=r["created_at"], ended_at=r["ended_at"])


def _result(r) -> BenchResult:
    return BenchResult(id=r["id"], run_id=r["run_id"], provider=r["provider"], model=r["model"], case_id=r["case_id"],
                       repeat=r["repeat"], status=r["status"], by_hand=r["by_hand"], reasons=json.loads(r["reasons_json"]),
                       calls=json.loads(r["calls_json"]), calls_made=r["calls_made"], answer=r["answer"],
                       thinking=r["thinking"] or "", turns=r["turns"],
                       input_tokens=r["input_tokens"], output_tokens=r["output_tokens"], duration_ms=r["duration_ms"],
                       created_at=r["created_at"])


async def create(conn, set_: CaseSet, models: builtins.list[ModelRef], repeats: int, note: str = "",
                 thinking_off: bool = False) -> BenchRun:
    """A bench that is running. The cases are kept as they are now: the set's file can change afterwards."""
    rid = new_id()
    await conn.execute(
        "insert into bench_runs(id,set_id,set_name,repeats,models_json,cases_json,status,note,created_at)"
        " values(?,?,?,?,?,?,?,?,?)",
        (rid, set_.id, set_.name, repeats, json.dumps([m.model_dump() for m in models]),
         json.dumps([c.model_dump() for c in briefs(set_)]), "running", note, now()))
    if thinking_off:
        await conn.execute("insert into bench_options(run_id,options_json) values(?,?)", (rid, json.dumps({"thinking_off": True})))
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
    if t is not None and t.thinking:
        end = t.thinking if len(t.thinking) <= THINKING_CHARS else "…" + t.thinking[-THINKING_CHARS:]
        await conn.execute("insert into bench_thinking(result_id,text) values(?,?)", (rid, end))
    await conn.commit()
    return await result(conn, rid)


async def result(conn, result_id: str) -> BenchResult | None:
    r = await (await conn.execute(f"{RESULTS} where r.id=?", (result_id,))).fetchone()
    return _result(r) if r else None


async def set_mark(conn, result_id: str, status: Literal["pass", "fail"] | None) -> BenchResult | None:
    """The owner's own mark for one run. None for `status`, and the mark the rules gave, take his mark away again:
    a correction that says what the rules say is none. Returns the result as it is now, None when there is no such
    result. NotJudged for a run the rules gave no mark."""
    was = await result(conn, result_id)
    if was is None:
        return None
    if was.status not in ("pass", "fail"):
        raise NotJudged(result_id)
    if status is None or status == was.status:
        await conn.execute("delete from bench_corrections where result_id=?", (result_id,))
    else:
        await conn.execute("insert into bench_corrections(result_id,status,created_at) values(?,?,?)"
                           " on conflict(result_id) do update set status=excluded.status, created_at=excluded.created_at",
                           (result_id, status, now()))
    await conn.commit()
    return await result(conn, result_id)


async def note_tools(conn, run_id: str, offered: int) -> None:
    await conn.execute("update bench_runs set tools_offered=? where id=?", (offered, run_id))
    await conn.commit()


async def finish(conn, run_id: str, status: str, note: str = "") -> BenchRun:
    await conn.execute("update bench_runs set status=?, note=?, ended_at=? where id=?", (status, note, now(), run_id))
    await conn.commit()
    return await get(conn, run_id)


async def get(conn, run_id: str) -> BenchRun | None:
    r = await (await conn.execute(f"{RUNS} where b.id=?", (run_id,))).fetchone()
    return _run(r) if r else None


async def results(conn, run_id: str) -> builtins.list[BenchResult]:
    """The results of a bench in the order they were added."""
    rows = await (await conn.execute(f"{RESULTS} where r.run_id=? order by r.rowid", (run_id,))).fetchall()
    return [_result(r) for r in rows]


async def marks(conn, run_id: str) -> builtins.list[Mark]:
    """Every result of a bench in the order they were added, as the table needs them."""
    rows = await (await conn.execute(
        "select r.provider, r.model, r.case_id, r.repeat, r.status, c.status as by_hand, r.turns, r.input_tokens,"
        " r.output_tokens, r.duration_ms from bench_results r left join bench_corrections c on c.result_id = r.id"
        " where r.run_id=? order by r.rowid", (run_id,))).fetchall()
    return [Mark(**dict(r)) for r in rows]


async def cell(conn, run_id: str, ref: ModelRef, case_id: str) -> builtins.list[BenchResult]:
    """The runs of one case on one model, by repeat."""
    rows = await (await conn.execute(
        f"{RESULTS} where r.run_id=? and r.provider=? and r.model=? and r.case_id=? order by r.repeat, r.rowid",
        (run_id, ref.provider, ref.model, case_id))).fetchall()
    return [_result(r) for r in rows]


async def list(conn) -> builtins.list[BenchRun]:
    """Every bench, the newest first."""
    rows = await (await conn.execute(f"{RUNS} order by b.created_at desc, b.rowid desc")).fetchall()
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
