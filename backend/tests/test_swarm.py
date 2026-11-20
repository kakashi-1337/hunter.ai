"""swarm: several scoped subagents at the same time, for cloud models only."""
import asyncio
import json

import pytest

from hunterai import subagents
from hunterai.providers import registry
from hunterai.providers.base import ModelInfo, ProviderError, Stop, TextDelta, ToolCall, Usage
from hunterai.repo import settings
from hunterai.tools import registry as tools
from tests.test_engine import setup, use  # noqa: F401 (setup is a fixture)
from tests.test_subagents import go, result_of
from tests.toolkit import fake_tool, slow_connections


def task(key, tool="t_echo"):
    return {"brief": f"Look at host {key}", "success_criteria": "an answer", "tools": [tool]}


class Hive:
    """The chat model: the orchestrator calls swarm once; each subagent runs one tool, then answers.
    It counts how many subagent calls are in flight at the same moment."""
    supports_tools = True

    def __init__(self, name, tasks, fail=(), crowd=0):
        self.name, self.tasks, self.fail = name, tasks, set(fail)
        self.active = self.peak = 0
        self.crowd, self.full = crowd, asyncio.Event()      # crowd: hold the subagents until that many are in flight
        self.parent_calls = 0

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="m", label="m")]

    async def stream(self, model, system, messages, options, tools=None):
        if subagents.SUBAGENT_NOTE not in system:
            self.parent_calls += 1
            if self.parent_calls == 1:
                yield ToolCall("s1", "swarm", {"tasks": self.tasks})
            else:
                yield TextDelta("The swarm is done.")
        else:
            key = messages[0].blocks[0].text.split("Look at host ", 1)[1].split()[0]
            if key in self.fail:
                raise ProviderError("unavailable", f"no answer for {key}")
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.crowd:                  # so that the count does not hang on how fast this machine starts them
                if self.active >= self.crowd:
                    self.full.set()
                await asyncio.wait_for(self.full.wait(), 5)
            await asyncio.sleep(0.03)
            self.active -= 1
            if len(messages) == 1:
                yield ToolCall(f"e-{key}", "t_echo", {"q": key})
            else:
                yield TextDelta(f"host {key} answers")
        yield Usage(3, 5)
        yield Stop("end")


class Judge:
    """The decision model: says a task is worth it and accepts every answer."""

    def __init__(self, name):
        self.name, self.calls = name, 0

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="m", label="m")]

    async def stream(self, model, system, messages, options):
        self.calls += 1
        asked = system + " ".join(b.text for m in messages for b in m.blocks if getattr(b, "text", None))
        yield TextDelta(json.dumps({"verdict": {"accept": 0.9, "recheck": 0.05, "reject": 0.05}, "criteria_met": 0.9}
                                   if "criteria_met" in asked else {"worth": 0.9}))
        yield Usage(1, 1)
        yield Stop("end")


@pytest.fixture
async def hive(conn, setup):
    tools.register(subagents.tool())
    tools.register(subagents.swarm_tool())
    echo, ran = fake_tool("t_echo", text="HTTP 200")
    tools.register(echo)
    tools.register(fake_tool("browser_navigate", text="a page")[0])
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    registry.override("decider", Judge("decider"))

    async def run(tasks, provider="abliteration", **kw):
        p = Hive(provider, tasks, **kw)
        use(**{provider: p})
        got, msg = await go(conn, *await setup(provider=provider))
        return p, got, msg
    return type("H", (), {"run": staticmethod(run), "ran": ran})


async def test_a_swarm_runs_three_subagents_at_a_time_and_judges_each_answer(conn, hive):
    p, run, msg = await hive.run([task(k) for k in "abcde"])
    assert run.status == "done", run.error
    got = result_of(msg)
    assert [r["task"] for r in got["results"]] == [1, 2, 3, 4, 5]
    assert [r["answer"] for r in got["results"]] == [f"host {k} answers" for k in "abcde"]
    assert all(r["verified"] and r["verdict"] == "accept" and r["calls"] == 1 for r in got["results"]) and "5 of 5" in got["note"]
    assert p.peak == subagents.SWARM_WIDTH and sorted(c["q"] for c in hive.ran) == list("abcde")
    members = [s for s in run.steps if s.kind == "subagent"]
    assert [s.name for s in members] == [f"subagent {n} of 5" for n in range(1, 6)] and all(s.status == "done" and s.ended_at for s in members)
    assert all(s.detail["verified"] is True for s in members)
    for s in members:                                   # each member's own steps hang under its step
        kinds = [c.kind for c in run.steps if c.parent_step_id == s.id]
        assert kinds.count("model_call") == 2 and kinds.count("tool_call") == 1 and kinds.count("decision") == 2
    assert len({s.seq for s in run.steps}) == len(run.steps)       # no two steps share a number


async def test_a_swarm_is_refused_for_a_local_decision_model(conn, hive):
    """A cloud chat whose decision model is local: no swarm, and `delegate` is named instead. A chat whose own model
    is local is offered neither tool (tests/test_local_alone.py)."""
    await settings.put(conn, "decision_provider", "ollama")
    await settings.put(conn, "decision_model", "qwen3:8b")
    p, run, msg = await hive.run([task("a"), task("b")])
    block = next(b for b in msg.blocks if b.type == "tool_result")
    assert block.is_error and "cloud decision model" in block.content and "ollama/qwen3:8b" in block.content and hive.ran == []


async def test_a_member_has_no_browser_no_local_fallback_and_fails_alone(conn, hive):
    local = Hive("ollama", [])
    use(ollama=local)
    p, run, msg = await hive.run([task("a"), task("b", tool="browser_navigate"), task("c")], fail={"c"})
    got = result_of(msg)["results"]
    assert got[0]["verified"] is True
    assert got[1]["status"] == "error" and "none of the requested tools" in got[1]["error"] and "browser_navigate" not in got[1]["error"]
    assert got[2]["status"] == "error" and got[2]["verified"] is False and "no answer for c" in got[2]["error"]
    assert local.peak == 0 and local.parent_calls == 0                 # the local model was never asked to stand in
    assert "1 of 3" in result_of(msg)["note"] and run.status == "done"


async def test_a_swarm_counts_against_the_runs_subagents(conn, hive, monkeypatch):
    monkeypatch.setattr(subagents, "MAX_SUBAGENTS_PER_RUN", 2)
    p, run, msg = await hive.run([task(k) for k in "abc"])
    block = next(b for b in msg.blocks if b.type == "tool_result")
    assert block.is_error and "at most 2 subagents" in block.content and "2 are left" in block.content and hive.ran == []


@pytest.mark.parametrize("setting,peak", [(5, 5), (1, 1), (99, 6), ("junk", 3)])
async def test_the_owners_setting_decides_how_many_work_at_the_same_time(conn, hive, setting, peak):
    """One to six, six being the tasks one swarm holds; a setting that is no number leaves the three."""
    await settings.put(conn, "swarm_width", setting)
    p, run, msg = await hive.run([task(k) for k in "abcdef"], crowd=peak)
    assert run.status == "done", run.error
    assert (p.peak, [r["status"] for r in result_of(msg)["results"]]) == (peak, ["done"] * 6)


async def test_members_that_finish_together_do_not_trip_over_one_connection(conn, hive, monkeypatch):
    """Six members close their steps one right after the other while another part of the app is writing. Their closings
    shared one connection and overlapped on it: one member's read was still open when another's update began, and SQLite
    answers a write that starts from an open read with "database is locked" at once, whatever the busy timeout."""
    from hunterai import db
    await settings.put(conn, "swarm_width", 6)
    other = await db.connect()                              # the rest of the app: another chat, the ingest worker
    slow_connections(monkeypatch, delay=0, fetch=0.01)      # a busy machine: time passes before the rows of a statement are read
    waiting, all_here, held = [], asyncio.Event(), asyncio.Event()
    work = subagents._one

    async def done_one_after_the_other(ctx, args, swarm=False):
        out = await work(ctx, args, swarm=swarm)
        place = len(waiting)
        waiting.append(ctx.step_id)
        if len(waiting) == 6:
            all_here.set()
        await asyncio.wait_for(all_here.wait(), 5)
        await asyncio.wait_for(held.wait(), 5)              # the other writer has the database now
        await asyncio.sleep(0.005 * place)                  # each is done while the one before it is half-way through closing
        return out
    monkeypatch.setattr(subagents, "_one", done_one_after_the_other)

    async def writes_meanwhile():
        await all_here.wait()
        await other.execute("begin immediate")
        await other.execute("update settings set value_json=value_json where key='swarm_width'")
        held.set()
        await asyncio.sleep(0.08)                           # far below the busy timeout: a write that waits gets its turn
        await other.commit()
    writer = asyncio.create_task(writes_meanwhile())
    try:
        p, run, msg = await hive.run([task(k) for k in "abcdef"], crowd=6)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
        await other.close()
    answer = next(b for b in msg.blocks if b.type == "tool_result")
    assert len(waiting) == 6 and not answer.is_error, answer.content
    assert run.status == "done" and [r["status"] for r in result_of(msg)["results"]] == ["done"] * 6
