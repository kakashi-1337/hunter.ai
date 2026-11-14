import json

from hunterai import activity
from hunterai.repo import runs
from tests.test_api import client  # noqa: F401 (client is a fixture)
from tests.toolkit import seeded_run


async def a_run(conn):
    chat, run_id = await seeded_run(conn)
    m = await runs.add_step(conn, run_id, "model_call", "abliteration/big", {})
    await runs.finish_step(conn, m.id, "done", 1200, 80, {"stop_reason": "tool_calls"})
    t = await runs.add_step(conn, run_id, "tool_call", "shell", {"tool_call_id": "c1", "arguments": {"command": "cat /etc/secret"}})
    await runs.patch_step(conn, t.id, status="done", close=True, detail={"result_preview": "TOP SECRET CONTENT"})
    t2 = await runs.add_step(conn, run_id, "tool_call", "shell", {"tool_call_id": "c2"})
    await runs.patch_step(conn, t2.id, status="error", close=True, detail={"error": "exit 1: password=hunter2"})
    m2 = await runs.add_step(conn, run_id, "model_call", "abliteration/big", {})
    await runs.finish_step(conn, m2.id, "error", 0, 0, {"error": "abliteration timed out", "kind": "timeout"})
    d = await runs.add_step(conn, run_id, "decision", "risk gate", {})
    await runs.patch_step(conn, d.id, status="done", close=True, detail={"model": "haiku"})
    await runs.add_step(conn, run_id, "tool_call", "web_fetch", {})            # still running: not counted as finished
    await runs.finish(conn, run_id, "done")
    return run_id


async def test_every_finished_step_is_one_line_in_the_activity_log_without_content(conn):
    run_id = await a_run(conn)
    lines = [json.loads(x) for x in activity.log_file().read_text().splitlines()]
    assert [(x["kind"], x["name"], x["status"]) for x in lines] == [
        ("model_call", "abliteration/big", "done"), ("tool_call", "shell", "done"), ("tool_call", "shell", "error"),
        ("model_call", "abliteration/big", "error"), ("decision", "risk gate", "done")]
    assert lines[0]["in"] == 1200 and lines[0]["out"] == 80 and lines[0]["run"] == run_id and lines[0]["ms"] >= 0 and lines[0]["at"]
    assert lines[3]["error"] == "abliteration timed out"                     # why a model call failed is kept
    text = activity.log_file().read_text()
    assert "SECRET" not in text and "hunter2" not in text and "cat /etc" not in text      # no arguments, no results, no tool errors


async def test_the_log_can_be_switched_off_and_is_rotated(conn, monkeypatch):
    monkeypatch.setattr(activity, "MAX_BYTES", 400)
    await a_run(conn); await a_run(conn)
    assert activity.log_file().stat().st_size <= 800 and activity.log_file().with_suffix(".jsonl.1").exists()
    monkeypatch.setenv("HUNTERAI_ACTIVITY_LOG", "off")
    before = activity.log_file().read_text()
    await a_run(conn)
    assert activity.log_file().read_text() == before


async def test_the_summary_counts_calls_tokens_and_failures(client, conn):
    await a_run(conn); await a_run(conn)
    got = (await client.get("/api/activity?days=7")).json()
    assert got["totals"] == {"runs": 2, "model_calls": 4, "tool_calls": 4, "input_tokens": 2400, "output_tokens": 160, "errors": 4}
    assert [(m["name"], m["calls"], m["errors"], m["input_tokens"]) for m in got["models"]] == [("abliteration/big", 4, 2, 2400)]
    assert [(t["name"], t["calls"], t["errors"]) for t in got["tools"]] == [("shell", 4, 2)]
    assert got["decisions"]["calls"] == 2 and len(got["days"]) == 1 and got["days"][0]["runs"] == 2
    assert got["days"][0]["input_tokens"] == 2400 and got["log_file"].endswith("activity.jsonl")
    assert (await client.get("/api/activity?days=0")).status_code == 422
