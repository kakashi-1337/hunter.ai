"""A record of what HunterAI did, kept on this machine and sent nowhere.

Two things. A log file, `~/.hunterai/logs/activity.jsonl`: one line per finished step of a
run (a model call, a tool call, a decision, a permission answer, a fallback, a retrieval)
with its time, name, outcome, duration and token counts. It holds no arguments, no results,
and no tool error text, so it can be kept and shared without leaking a target's data, and it
outlives the chats (deleting a chat deletes its trajectory, not these lines). And a summary
for the interface, counted from the trajectories in the database.

`HUNTERAI_ACTIVITY_LOG=off` stops the log. Nothing here ever raises into a run."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

MAX_BYTES = 20 * 1024**2          # then the file becomes activity.jsonl.1 and a new one starts
KEEP_ERROR_OF = ("model_call", "fallback", "decision")      # their errors say what a provider did, not what a target said


def log_file() -> Path:
    return config.logs_dir() / "activity.jsonl"


def record(step) -> None:
    """One line for a step that has just ended."""
    try:
        if (os.environ.get("HUNTERAI_ACTIVITY_LOG") or "").lower() == "off" or not step or not step.ended_at:
            return
        ms = max(0, int((datetime.fromisoformat(step.ended_at) - datetime.fromisoformat(step.started_at)).total_seconds() * 1000))
        line = {"at": step.ended_at, "run": step.run_id, "kind": step.kind, "name": step.name, "status": step.status, "ms": ms}
        if step.input_tokens or step.output_tokens:
            line.update({"in": step.input_tokens, "out": step.output_tokens})
        error = (step.detail or {}).get("error")
        if step.kind in KEEP_ERROR_OF and isinstance(error, str) and error:
            line["error"] = error[:200]
        path = log_file()
        try:
            if path.stat().st_size >= MAX_BYTES:
                path.replace(path.with_suffix(".jsonl.1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - a full disk or an odd timestamp must not touch the run
        pass


SECONDS = "(julianday(s.ended_at) - julianday(s.started_at)) * 86400.0"


async def summary(conn, days: int) -> dict:
    """What happened in the last `days` days, from the trajectories that still exist."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    async def rows(sql: str, *args):
        return await (await conn.execute(sql, args)).fetchall()

    def group(kind: str) -> str:
        return (f"select s.name as name, count(*) as calls, sum(s.status='error') as errors, sum(s.input_tokens) as i, "
                f"sum(s.output_tokens) as o, avg({SECONDS}) as secs from steps s where s.kind='{kind}' and s.ended_at is not null "
                "and s.started_at >= ? group by s.name order by calls desc, name limit 40")
    models = [{"name": r["name"], "calls": r["calls"], "errors": r["errors"] or 0, "input_tokens": r["i"] or 0,
               "output_tokens": r["o"] or 0, "avg_seconds": round(r["secs"] or 0, 1)} for r in await rows(group("model_call"), since)]
    tools = [{"name": r["name"], "calls": r["calls"], "errors": r["errors"] or 0, "avg_seconds": round(r["secs"] or 0, 1)}
             for r in await rows(group("tool_call"), since)]
    d = (await rows(f"select count(*) as calls, sum(s.status='error') as errors, avg({SECONDS}) as secs from steps s "
                    "where s.kind='decision' and s.ended_at is not null and s.started_at >= ?", since))[0]
    per_day = await rows(
        "select substr(s.started_at, 1, 10) as day, count(distinct s.run_id) as runs, sum(s.kind='model_call') as m, "
        "sum(s.kind='tool_call') as t, sum(s.input_tokens) as i, sum(s.output_tokens) as o, sum(s.status='error') as e "
        "from steps s where s.ended_at is not null and s.started_at >= ? group by day order by day desc limit 60", since)
    n_runs = (await rows("select count(distinct run_id) as n from steps where ended_at is not null and started_at >= ?", since))[0]["n"]
    return {
        "days_asked": days,
        "totals": {"runs": n_runs, "model_calls": sum(m["calls"] for m in models), "tool_calls": sum(t["calls"] for t in tools),
                   "input_tokens": sum(m["input_tokens"] for m in models), "output_tokens": sum(m["output_tokens"] for m in models),
                   "errors": sum(m["errors"] for m in models) + sum(t["errors"] for t in tools)},
        "models": models, "tools": tools,
        "decisions": {"calls": d["calls"] or 0, "errors": d["errors"] or 0, "avg_seconds": round(d["secs"] or 0, 1)},
        "days": [{"day": r["day"], "runs": r["runs"], "model_calls": r["m"] or 0, "tool_calls": r["t"] or 0,
                  "input_tokens": r["i"] or 0, "output_tokens": r["o"] or 0, "errors": r["e"] or 0} for r in per_day],
        "log_file": str(log_file()),
    }
