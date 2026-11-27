"""A judge model is checked before it is trusted: it is asked about the answers of a set whose right verdict is
known, and what it said is counted. One check is kept per set and judge. One thing at a time: no check while a
bench or a judging runs."""
import asyncio

import pytest

from hunterai import cooling, db, thermal
from hunterai.bench import cases, judge, judging, runner
from hunterai.providers import registry
from hunterai.providers.base import ProviderError, TextDelta, Usage
from hunterai.repo import bench as store
from hunterai.repo.bench import ModelRef
from hunterai.tools import native
from tests.bench_model import KNOWN_TWO as TWO
from tests.bench_model import Held, Model, says

A, JUDGE = ModelRef(provider="scripted", model="a"), ModelRef(provider="judge", model="j")
HOT = thermal.Part(thermal.GRAPHICS, 88.0, 85.0, "HUNTERAI_GPU_LIMIT")
PLAIN = 'name = "Plain"\n[[case]]\nid = "one"\ngroup = "honesty"\nprompt = "x?"\n[case.expect]\nanswer_any = ["y"]\n'
DOWN = [ProviderError("unavailable", "Ollama not reachable")]


def verdict(word: str, reason: str = "Because.") -> list:
    return [TextDelta(f'{{"verdict": "{word}", "reason": "{reason}"}}'), Usage(30, 8)]


@pytest.fixture(autouse=True)
async def a_set_of_its_own(isolated_home):
    native.register_all()
    cases.folder().mkdir(parents=True)
    (cases.folder() / "two.toml").write_text(TWO)
    yield
    await asyncio.wait_for(judging.shutdown(), 30)
    await asyncio.wait_for(runner.shutdown(), 30)
    assert not judging.busy() and runner.running() is None


def use(*turns, name="judge", cls=Model):
    model = cls(*turns)
    registry.override(name, model)
    return model


async def checked(ref=JUDGE, set_id="own:two"):
    started = await judging.start_check(db.connect, set_id, ref)
    await asyncio.wait_for(judging.wait_check(started.id), 30)
    return started


# ---- what is kept (the store)

async def test_a_check_keeps_what_was_known_and_what_the_judge_said_and_counts_it(conn):
    two = cases.load("own:two")
    made = await store.start_check(conn, two, JUDGE, 4)
    assert (made.set_id, made.set_name, made.provider, made.model, made.status, made.total) == ("own:two", "Two", "judge", "j", "running", 4)
    assert (made.done, made.correct, made.passed_wrongly, made.failed_wrongly, made.unanswered) == (0, 0, 0, 0, 0)
    await store.add_checked(conn, made.id, "record", 1, "pass", "There is no record of it.", "pass", "It says so.")
    await store.add_checked(conn, made.id, "record", 2, "fail", "It is a buffer overflow.", "pass", "Looks fine.")
    await store.add_checked(conn, made.id, "sum", 1, "pass", "It is 42.", "fail", "Wrong number.")
    await store.add_checked(conn, made.id, "sum", 2, "fail", "It is 43.", None, error="Ollama not reachable")
    now = await store.check(conn, made.id)
    assert (now.done, now.correct, now.passed_wrongly, now.failed_wrongly, now.unanswered) == (4, 1, 1, 1, 1)
    await store.add_checked(conn, made.id, "sum", 3, "fail", "It is 44.", "pass", "Close enough.")       # a second fail called a pass
    now = await store.check(conn, made.id)
    assert (now.done, now.correct, now.passed_wrongly, now.failed_wrongly, now.unanswered) == (5, 1, 2, 1, 1)
    await conn.execute("delete from bench_checked_answers where check_id=? and case_id='sum' and n=3", (made.id,))
    await conn.commit()
    assert [a.model_dump() for a in await store.checked(conn, made.id)] == [
        {"case_id": "record", "n": 1, "expected": "pass", "verdict": "pass", "reason": "It says so.", "error": None, "answer": "There is no record of it."},
        {"case_id": "record", "n": 2, "expected": "fail", "verdict": "pass", "reason": "Looks fine.", "error": None, "answer": "It is a buffer overflow."},
        {"case_id": "sum", "n": 1, "expected": "pass", "verdict": "fail", "reason": "Wrong number.", "error": None, "answer": "It is 42."},
        {"case_id": "sum", "n": 2, "expected": "fail", "verdict": None, "reason": "", "error": "Ollama not reachable", "answer": "It is 43."}]
    await store.finish_check(conn, made.id, "stopped", "The judge could not be asked.")
    over = await store.check(conn, made.id)
    assert (over.status, over.note, over.ended_at is not None) == ("stopped", "The judge could not be asked.", True)
    assert await store.check(conn, "nothing") is None and await store.checked(conn, "nothing") == []


async def test_one_check_is_kept_for_a_set_and_a_judge(conn):
    two = cases.load("own:two")
    first = await store.start_check(conn, two, JUDGE, 3)
    await store.add_checked(conn, first.id, "sum", 1, "pass", "It is 42.", "pass")
    other = await store.start_check(conn, two, ModelRef(provider="judge", model="k"), 3)
    elsewhere = await store.start_check(conn, cases.load("starter"), JUDGE, 54)        # the same judge on another set
    again = await store.start_check(conn, two, JUDGE, 3)          # the same judge on the same set: the one before is gone
    assert again.id != first.id and await store.check(conn, first.id) is None and await store.checked(conn, first.id) == []
    assert [c.id for c in await store.checks(conn)] == [again.id, elsewhere.id, other.id]        # the newest first
    assert [c.id for c in await store.checks(conn, "own:two")] == [again.id, other.id]
    assert [c.id for c in await store.checks(conn, "starter")] == [elsewhere.id] and await store.checks(conn, "own:nothing") == []


async def test_a_check_that_a_stop_of_hunterai_cut_off_is_marked_at_the_next_start(conn):
    made = await store.start_check(conn, cases.load("own:two"), JUDGE, 3)
    await judging.recover(conn)
    now = await store.check(conn, made.id)
    assert (now.status, now.note, now.ended_at is not None) == ("interrupted", "HunterAI stopped while this check was running.", True)


# ---- a check asks the judge about every known answer

async def test_a_check_asks_the_judge_about_every_known_answer_as_a_judging_would(conn):
    model = use(verdict("pass", "It says so."), verdict("pass", "Looks fine."), verdict("fail", "Wrong number."), verdict("fail", "It is 43."))
    started = await checked()
    assert (started.status, started.total, started.set_id, started.set_name, started.model) == ("running", 4, "own:two", "Two", "j")
    now = await store.check(conn, started.id)
    assert (now.status, now.note, now.done, now.correct, now.passed_wrongly, now.failed_wrongly, now.unanswered) == ("done", "", 4, 2, 1, 1, 0)
    assert [(a.case_id, a.n, a.expected, a.verdict, a.reason, a.answer) for a in await store.checked(conn, started.id)] == [
        ("record", 1, "pass", "pass", "It says so.", "There is no record of it."),
        ("record", 2, "fail", "pass", "Looks fine.", "It is a buffer overflow."),
        ("sum", 1, "pass", "fail", "Wrong number.", "It is 42."),
        ("sum", 2, "fail", "fail", "It is 43.", "It is 43.")]
    record = cases.load("own:two").cases[0]
    asked = [seen["messages"][0].blocks[0].text for seen in model.seen]
    assert asked[0] == judge.question(record, [store.CallBrief(name="cve", arguments='{"action": "lookup"}')], "There is no record of it.")
    assert "-> No record was found." in asked[0] and "The assistant called no tool." in asked[1] and "what is 19 + 23?" in asked[2]
    assert (model.seen[0]["model"], model.seen[0]["system"]) == ("j", judge.SYSTEM) and not judging.busy()


async def test_a_reply_without_a_verdict_is_counted_apart_and_the_check_goes_on(conn):
    prose = [TextDelta("I would say it passes."), Usage(1, 1)]
    use(prose, prose, prose, verdict("fail"))
    started = await checked()
    now = await store.check(conn, started.id)
    assert (now.status, now.done, now.correct, now.unanswered, now.note) == ("done", 4, 1, 3, "")
    assert [a.error for a in await store.checked(conn, started.id)] == [judge.NO_VERDICT] * 3 + [None]


async def test_a_check_is_refused_with_the_reason(conn):
    use(verdict("pass"))
    with pytest.raises(KeyError):
        await judging.start_check(db.connect, "own:nothing", JUDGE)
    with pytest.raises(ValueError, match="unknown provider nobody"):
        await judging.start_check(db.connect, "own:two", ModelRef(provider="nobody", model="j"))
    (cases.folder() / "plain.toml").write_text(PLAIN)
    with pytest.raises(ValueError, match="This set has no answers whose right verdict is known"):
        await judging.start_check(db.connect, "own:plain", JUDGE)
    (cases.folder() / "bad.toml").write_text("name = 3")
    with pytest.raises(cases.CaseError):
        await judging.start_check(db.connect, "own:bad", JUDGE)
    assert await store.checks(conn) == [] and not judging.busy()


# ---- one thing at a time

async def test_a_check_on_its_way_counts_as_one_that_runs(conn):
    use(verdict("pass"))
    reached, go = asyncio.Event(), asyncio.Event()

    async def slow_connect():
        reached.set()
        await go.wait()
        return await db.connect()

    starting = asyncio.create_task(judging.start_check(slow_connect, "own:two", JUDGE))
    await asyncio.wait_for(reached.wait(), 20)
    try:
        on_its_way = judging.checking()
        assert on_its_way is not None and judging.busy() and judging.running() is None and judging.check_progress(on_its_way) is None
        with pytest.raises(judging.Busy):
            await judging.start_check(db.connect, "own:two", JUDGE)
    finally:
        go.set()
        started = await asyncio.wait_for(starting, 30)
        await asyncio.wait_for(judging.wait_check(started.id), 30)
    assert started.id == on_its_way and judging.checking() is None


async def test_while_a_check_runs_no_other_begins_and_no_judging(conn):
    held = use(verdict("pass"), cls=Held)
    started = await judging.start_check(db.connect, "own:two", JUDGE)
    await asyncio.wait_for(held.reached.wait(), 20)
    assert judging.busy() and judging.checking() == started.id and judging.running() is None
    assert judging.check_progress(started.id) == {"done": 0, "total": 4, "doing": "running"}
    assert judging.check_progress("another") is None and judging.progress(started.id) is None
    with pytest.raises(judging.Busy):
        await judging.start_check(db.connect, "own:two", ModelRef(provider="judge", model="k"))
    with pytest.raises(judging.Busy):
        await judging.start(db.connect, "a bench", JUDGE)
    assert await judging.cancel(started.id) is False            # it is no judging of a bench
    held.release()
    await asyncio.wait_for(judging.wait_check(started.id), 30)
    assert not judging.busy() and judging.checking() is None and judging.check_progress(started.id) is None
    assert (await store.check(conn, started.id)).status == "done"


async def test_no_check_begins_while_a_bench_runs_or_a_judge_reads_one(conn):
    slow = use(says("there is no record, and it is 42"), name="scripted", cls=Held)
    going = await runner.start(db.connect, [A], "own:two", 1)
    await asyncio.wait_for(slow.reached.wait(), 20)
    use(verdict("pass"))
    with pytest.raises(judging.Busy):
        await judging.start_check(db.connect, "own:two", JUDGE)
    slow.release()
    await asyncio.wait_for(runner.wait(going.id), 30)
    held = use(verdict("pass"), cls=Held)
    await judging.start(db.connect, going.id, JUDGE)
    await asyncio.wait_for(held.reached.wait(), 20)
    with pytest.raises(judging.Busy):
        await judging.start_check(db.connect, "own:two", JUDGE)
    assert judging.checking() is None and await judging.cancel_check(going.id) is False
    held.release()
    await asyncio.wait_for(judging.wait(going.id), 30)


# ---- how it ends early

async def test_a_cancelled_check_keeps_what_it_asked(conn):
    held = use(verdict("pass"), cls=Held)
    started = await judging.start_check(db.connect, "own:two", JUDGE)
    await asyncio.wait_for(held.reached.wait(), 20)
    await held.step()                                     # the first verdict is in, the second answer is being asked about
    assert judging.check_progress(started.id) == {"done": 1, "total": 4, "doing": "running"}
    assert await judging.cancel_check("another") is False and await judging.cancel_check(started.id) is True
    await asyncio.wait_for(judging.wait_check(started.id), 30)
    now = await store.check(conn, started.id)
    assert (now.status, now.note, now.done, now.total, now.correct) == ("cancelled", "", 1, 4, 1)
    assert await judging.cancel_check(started.id) is False


async def test_a_judge_that_cannot_be_asked_three_times_in_a_row_ends_the_check(conn):
    model = use(DOWN)
    started = await checked()
    now = await store.check(conn, started.id)
    assert (now.status, now.done, now.total, now.unanswered, len(model.seen)) == ("stopped", 3, 4, 3, 3)
    assert now.note == "The judge could not be asked three times in a row: Ollama not reachable"


async def test_a_stop_of_hunterai_ends_the_check_as_interrupted(conn):
    held = use(verdict("pass"), cls=Held)
    started = await judging.start_check(db.connect, "own:two", JUDGE)
    await asyncio.wait_for(held.reached.wait(), 20)
    await asyncio.wait_for(judging.shutdown(), 30)
    now = await store.check(conn, started.id)
    assert (now.status, now.note, now.done) == ("interrupted", "HunterAI stopped while this check was running.", 0)
    assert not judging.busy()


async def test_a_judge_on_this_machine_waits_for_it_to_cool_in_a_check_too(conn, monkeypatch):
    waiting, cooled = asyncio.Event(), asyncio.Event()

    async def wait_cool(wait, step, margin, tell):
        if not cooled.is_set():
            await tell(HOT)
            waiting.set()
            await cooled.wait()

    monkeypatch.setattr(cooling, "here", lambda provider, model: True)
    monkeypatch.setattr(thermal, "wait_cool", wait_cool)
    held = use(verdict("pass"), cls=Held)
    started = await judging.start_check(db.connect, "own:two", JUDGE)
    await asyncio.wait_for(waiting.wait(), 20)
    assert judging.check_progress(started.id) == {"done": 0, "total": 4, "doing": "cooling"}
    cooled.set()
    await asyncio.wait_for(held.reached.wait(), 20)
    assert judging.check_progress(started.id)["doing"] == "running"
    held.release()
    await asyncio.wait_for(judging.wait_check(started.id), 30)


async def test_a_machine_that_stays_hot_ends_the_check_before_the_judge_is_asked(conn, monkeypatch):
    async def stays_hot(wait, step, margin, tell):
        await tell(HOT)
        return HOT

    monkeypatch.setattr(cooling, "here", lambda provider, model: True)
    monkeypatch.setattr(thermal, "wait_cool", stays_hot)
    model = use(verdict("pass"))
    started = await checked()
    now = await store.check(conn, started.id)
    assert (now.status, now.done, model.seen) == ("stopped", 0, [])
    assert now.note == ("The graphics card is at 88 °C and did not cool to 80 °C within 3 minutes (its limit is 85 °C). "
                        "The check was stopped before the judge j was asked. Let the machine cool and check the judge "
                        "again. HUNTERAI_GPU_LIMIT sets the limit in °C (off: no limit).")
