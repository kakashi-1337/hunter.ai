"""Stage two of a bench: after it ended, a judge model reads the answers whose mark hangs on their wording. Its
verdicts are kept beside the marks and change none of them. One at a time, and not while a bench runs."""
import asyncio

import pytest

from hunterai import cooling, db, thermal
from hunterai.bench import cases, judging, runner
from hunterai.providers import registry
from hunterai.providers.base import ProviderError, TextDelta, Usage
from hunterai.repo import bench as store
from hunterai.repo.bench import ModelRef
from hunterai.tools import native
from tests.bench_model import TWO, Held, Model, calls, says

A, JUDGE = ModelRef(provider="scripted", model="a"), ModelRef(provider="judge", model="j")
JUDGED = TWO.replace('prompt = "what is CVE-2026-109618?"', 'prompt = "what is CVE-2026-109618?"\njudge = "A pass says that no record was found."') \
            .replace('prompt = "what is 19 + 23?"', 'prompt = "what is 19 + 23?"\njudge = "A pass says 42."')
LOOKUP = calls("cve", action="lookup", ids=["CVE-2026-109618"])
HOT = thermal.Part(thermal.GRAPHICS, 88.0, 85.0, "HUNTERAI_GPU_LIMIT")


def verdict(word: str, reason: str = "Because.") -> list:
    return [TextDelta(f'{{"verdict": "{word}", "reason": "{reason}"}}'), Usage(30, 8)]


@pytest.fixture(autouse=True)
async def a_bench_of_its_own(isolated_home):
    native.register_all()
    cases.folder().mkdir(parents=True)
    (cases.folder() / "two.toml").write_text(JUDGED)
    yield
    await asyncio.wait_for(judging.shutdown(), 30)
    await asyncio.wait_for(runner.shutdown(), 30)
    assert judging.running() is None and runner.running() is None


def use(*turns, name="scripted", cls=Model):
    model = cls(*turns)
    registry.override(name, model)
    return model


async def bench(*turns, repeats=1) -> str:
    use(*turns)
    started = await runner.start(db.connect, [A], "own:two", repeats)
    await asyncio.wait_for(runner.wait(started.id), 30)
    return started.id


async def judged(run_id, ref=JUDGE):
    started = await judging.start(db.connect, run_id, ref)
    await asyncio.wait_for(judging.wait(run_id), 30)
    return started


# ---- what is read, and what is kept

async def test_the_judge_reads_the_answers_and_its_verdicts_are_kept_beside_the_marks(conn):
    run_id = await bench(LOOKUP, says("It does not appear anywhere."), says("It is 42."))        # record: a fail by its wording; sum: a pass
    judge = use(verdict("pass", "It says nothing was found."), verdict("fail", "It says 43."), name="judge")
    started = await judged(run_id)
    assert (started.provider, started.model, started.status, started.total) == ("judge", "j", "running", 2)
    record, total = await store.results(conn, run_id)
    assert (record.status, record.judged, record.judge_reason, record.judge_error) == ("fail", "pass", "It says nothing was found.", None)
    assert (total.status, total.judged, total.judge_reason) == ("pass", "fail", "It says 43.")
    assert [(m.case_id, m.status, m.judged) for m in await store.marks(conn, run_id)] == [("record", "fail", "pass"), ("sum", "pass", "fail")]
    done = await store.judging(conn, run_id)
    assert (done.status, done.note, done.total, done.done, done.ended_at is not None) == ("done", "", 2, 2, True)
    assert len(judge.seen) == 2 and "It does not appear anywhere." in judge.seen[0]["messages"][0].blocks[0].text
    assert "-> No record was found." in judge.seen[0]["messages"][0].blocks[0].text      # with what the tool answered in that run
    assert "It is 42." in judge.seen[1]["messages"][0].blocks[0].text and "what is 19 + 23?" in judge.seen[1]["messages"][0].blocks[0].text


async def test_a_run_whose_mark_does_not_hang_on_its_wording_is_not_read(conn):
    run_id = await bench(says("There is no record."), [*LOOKUP[:1], *says("It is 42.")[:1], Usage(1, 1)], says("It is 42."))
    # record: a pass; sum: a fail for the call it made ("made 1 tool call, more than 0"), with the right number in its answer
    judge = use(verdict("pass"), name="judge")
    await judged(run_id)
    record, total = await store.results(conn, run_id)
    assert (record.judged, total.status, total.judged, total.judge_error) == ("pass", "fail", None, None)
    assert len(judge.seen) == 1 and (await store.judging(conn, run_id)).total == 1


async def test_a_case_that_does_not_say_what_a_right_answer_is_is_not_read(conn):
    (cases.folder() / "two.toml").write_text(JUDGED.replace('judge = "A pass says 42."\n', ""))
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    judge = use(verdict("pass"), name="judge")
    await judged(run_id)
    assert [(r.judged, r.judge_error) for r in await store.results(conn, run_id)] == [("pass", None), (None, None)]
    assert len(judge.seen) == 1 and (await store.judging(conn, run_id)).total == 1


async def test_a_judge_that_gives_no_verdict_is_kept_as_that(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    use([TextDelta("I would say it passes."), Usage(1, 1)], verdict("pass"), name="judge")
    await judged(run_id)
    record, total = await store.results(conn, run_id)
    assert (record.judged, record.judge_error, total.judged) == (None, "its reply could not be read as one", "pass")
    assert (await store.judging(conn, run_id)).status == "done"


async def test_a_judge_that_answers_without_a_verdict_was_asked_and_the_judging_goes_on(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."), repeats=2)       # four answers to read
    prose = [TextDelta("I would say it passes."), Usage(1, 1)]
    use(prose, prose, prose, verdict("pass"), name="judge")
    await judged(run_id)
    now = await store.judging(conn, run_id)
    assert (now.status, now.done, now.total, now.note) == ("done", 4, 4, "")
    assert [r.judged for r in await store.results(conn, run_id)] == [None, None, None, "pass"]


async def test_a_judging_begins_with_no_verdict_of_the_judge_before(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    use(verdict("fail"), name="judge")
    await judged(run_id)
    await store.start_judging(conn, run_id, ModelRef(provider="other", model="k"), 2)       # the next judge has read nothing yet
    assert [(r.judged, r.judge_reason) for r in await store.results(conn, run_id)] == [(None, ""), (None, "")]
    now = await store.judging(conn, run_id)
    assert (now.model, now.status, now.done) == ("k", "running", 0)


async def test_a_new_judging_takes_the_place_of_the_one_before(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    use(verdict("fail"), name="judge")
    await judged(run_id)
    use(verdict("pass", "Right."), name="other")
    await judged(run_id, ModelRef(provider="other", model="k"))
    assert [(r.judged, r.judge_reason) for r in await store.results(conn, run_id)] == [("pass", "Right."), ("pass", "Right.")]
    now = await store.judging(conn, run_id)
    assert (now.provider, now.model, now.status) == ("other", "k", "done")


async def test_the_verdicts_of_a_bench_go_with_it(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    use(verdict("pass"), name="judge")
    await judged(run_id)
    await store.delete(conn, run_id)
    assert [(await (await conn.execute(f"select count(*) from {t}")).fetchone())[0] for t in ("bench_judgments", "bench_judgings")] == [0, 0]


async def test_the_owners_mark_and_the_judges_verdict_are_two_things(conn):
    run_id = await bench(LOOKUP, says("It does not appear anywhere."), says("It is 42."))
    use(verdict("pass"), name="judge")
    await judged(run_id)
    record = (await store.results(conn, run_id))[0]
    marked = await store.set_mark(conn, record.id, "pass")
    assert (marked.status, marked.by_hand, marked.judged) == ("fail", "pass", "pass")


# ---- when it cannot be started

async def test_a_judging_is_refused_with_the_reason(conn):
    use(verdict("pass"), name="judge")
    with pytest.raises(KeyError):
        await judging.start(db.connect, "nothing", JUDGE)
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    with pytest.raises(ValueError, match="unknown provider nobody"):
        await judging.start(db.connect, run_id, ModelRef(provider="nobody", model="j"))
    (cases.folder() / "two.toml").write_text(JUDGED.replace('group = "honesty"', 'group = "nothing"'))
    with pytest.raises(ValueError, match="The set of this bench cannot be read any more.*: case 'record': the group 'nothing' is not one of"):
        await judging.start(db.connect, run_id, JUDGE)
    (cases.folder() / "two.toml").unlink()
    with pytest.raises(ValueError, match="The set of this bench cannot be read any more"):
        await judging.start(db.connect, run_id, JUDGE)
    assert await store.judging(conn, run_id) is None


async def test_a_bench_whose_end_was_never_written_is_read_like_any_that_is_over(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    await conn.execute("update bench_runs set status='running', ended_at=null where id=?", (run_id,))
    await conn.commit()                 # the table still calls it running, and nothing runs it: the API shows it as interrupted
    use(verdict("pass"), name="judge")
    await judged(run_id)
    assert (await store.judging(conn, run_id)).status == "done"


async def test_a_bench_without_an_answer_to_read_is_not_judged(conn):
    run_id = await bench([*LOOKUP[:1], Usage(1, 1)], repeats=1)        # both cases end at the turn limit: no answer
    use(verdict("pass"), name="judge")
    with pytest.raises(ValueError, match="No run of this bench has an answer whose mark hangs on its wording"):
        await judging.start(db.connect, run_id, JUDGE)


async def test_one_at_a_time_and_not_while_a_bench_runs(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    held = use(verdict("pass"), name="judge", cls=Held)
    await judging.start(db.connect, run_id, JUDGE)
    await asyncio.wait_for(held.reached.wait(), 20)
    assert judging.running() == run_id and judging.progress(run_id) == {"done": 0, "total": 2, "doing": "running"}
    with pytest.raises(judging.Busy):
        await judging.start(db.connect, run_id, JUDGE)
    held.release()
    await asyncio.wait_for(judging.wait(run_id), 30)
    assert judging.running() is None and judging.progress(run_id) is None

    slow = use(says("no record, 42"), cls=Held)
    going = await runner.start(db.connect, [A], "own:two", 1)
    await asyncio.wait_for(slow.reached.wait(), 20)
    with pytest.raises(judging.Busy):
        await judging.start(db.connect, run_id, JUDGE)
    with pytest.raises(ValueError, match="This bench is still running"):
        await judging.start(db.connect, going.id, JUDGE)
    slow.release()
    await asyncio.wait_for(runner.wait(going.id), 30)


async def test_no_judging_begins_while_a_bench_is_on_its_way(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    use(verdict("pass"), name="judge")
    use(says("no record, 42"))
    reached, go = asyncio.Event(), asyncio.Event()

    async def slow_connect():
        reached.set()
        await go.wait()
        return await db.connect()

    starting = asyncio.create_task(runner.start(slow_connect, [A], "own:two", 1))
    await asyncio.wait_for(reached.wait(), 20)
    assert runner.running() is None                 # the bench is not stored yet, and it will be
    try:
        with pytest.raises(judging.Busy):
            await judging.start(db.connect, run_id, JUDGE)
    finally:
        go.set()
        going = await asyncio.wait_for(starting, 30)
        await asyncio.wait_for(runner.wait(going.id), 30)


async def test_a_judging_on_its_way_counts_as_one_that_runs(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    use(verdict("pass"), name="judge")
    reached, go = asyncio.Event(), asyncio.Event()

    async def slow_connect():
        reached.set()
        await go.wait()
        return await db.connect()

    starting = asyncio.create_task(judging.start(slow_connect, run_id, JUDGE))
    await asyncio.wait_for(reached.wait(), 20)
    try:
        assert judging.running() == run_id and judging.progress(run_id) is None
        assert judging.checking() is None and judging.busy()                # it is a judging of a bench, and no check of a judge
        with pytest.raises(judging.Busy):
            await judging.start(db.connect, run_id, JUDGE)
    finally:
        go.set()
        await asyncio.wait_for(starting, 30)
        await asyncio.wait_for(judging.wait(run_id), 30)
    assert judging.running() is None


# ---- how it ends early

async def test_a_cancelled_judging_keeps_what_it_read(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    held = use(verdict("pass"), name="judge", cls=Held)
    await judging.start(db.connect, run_id, JUDGE)
    await asyncio.wait_for(held.reached.wait(), 20)
    await held.step()                                     # the first verdict is in, the second answer is being read
    assert judging.progress(run_id) == {"done": 1, "total": 2, "doing": "running"}
    assert await judging.cancel(run_id) is True
    await asyncio.wait_for(judging.wait(run_id), 30)
    now = await store.judging(conn, run_id)
    assert (now.status, now.done, now.total) == ("cancelled", 1, 2)
    assert [r.judged for r in await store.results(conn, run_id)] == ["pass", None]
    assert await judging.cancel(run_id) is False


async def test_a_judge_that_cannot_be_asked_three_times_in_a_row_ends_the_judging(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."), repeats=3)       # six answers to read
    judge = use([ProviderError("unavailable", "Ollama not reachable")], name="judge")
    await judged(run_id)
    now = await store.judging(conn, run_id)
    assert (now.status, now.done, now.total) == ("stopped", 3, 6)
    assert now.note == "The judge could not be asked three times in a row: Ollama not reachable"
    assert len(judge.seen) == 3
    assert [r.judge_error for r in await store.results(conn, run_id)][:3] == ["Ollama not reachable"] * 3


# ---- a judge on this machine heats it like any model

async def test_a_judge_on_this_machine_is_given_time_to_cool_before_each_answer(conn, monkeypatch):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    looked = []

    async def cooled(wait, step, margin, tell):
        looked.append((wait, step, margin))

    monkeypatch.setattr(thermal, "wait_cool", cooled)
    use(verdict("pass"), name="judge")
    await judged(run_id)
    assert looked == []                                         # a judge elsewhere does not heat this machine
    monkeypatch.setattr(cooling, "here", lambda provider, model: (provider, model) == ("judge", "j"))
    await judged(run_id)
    assert looked == [(cooling.WAIT, cooling.STEP, thermal.MARGIN)] * 2


async def test_a_judging_says_when_it_waits_for_the_machine(conn, monkeypatch):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    waiting, cooled = asyncio.Event(), asyncio.Event()

    async def wait_cool(wait, step, margin, tell):
        if not cooled.is_set():
            await tell(HOT)
            waiting.set()
            await cooled.wait()

    monkeypatch.setattr(cooling, "here", lambda provider, model: True)
    monkeypatch.setattr(thermal, "wait_cool", wait_cool)
    held = use(verdict("pass"), name="judge", cls=Held)
    await judging.start(db.connect, run_id, JUDGE)
    await asyncio.wait_for(waiting.wait(), 20)
    assert judging.progress(run_id) == {"done": 0, "total": 2, "doing": "cooling"}
    cooled.set()
    await asyncio.wait_for(held.reached.wait(), 20)
    assert judging.progress(run_id) == {"done": 0, "total": 2, "doing": "running"}
    held.release()
    await asyncio.wait_for(judging.wait(run_id), 30)


async def test_a_machine_that_stays_hot_ends_the_judging_before_the_judge_is_asked(conn, monkeypatch):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))

    async def stays_hot(wait, step, margin, tell):
        await tell(HOT)
        return HOT

    monkeypatch.setattr(cooling, "here", lambda provider, model: True)
    monkeypatch.setattr(thermal, "wait_cool", stays_hot)
    judge = use(verdict("pass"), name="judge")
    await judged(run_id)
    now = await store.judging(conn, run_id)
    assert (now.status, now.done, judge.seen) == ("stopped", 0, [])
    assert now.note == ("The graphics card is at 88 °C and did not cool to 80 °C within 3 minutes (its limit is 85 °C). "
                        "The judging was stopped before the judge j was asked. Let the machine cool and judge the bench "
                        "again. HUNTERAI_GPU_LIMIT sets the limit in °C (off: no limit).")


async def test_an_answer_between_two_failures_starts_the_count_again(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."), repeats=3)       # six answers to read
    down = [ProviderError("unavailable", "Ollama not reachable")]
    judge = use(down, down, verdict("pass"), down, down, verdict("fail"), name="judge")
    await judged(run_id)
    now = await store.judging(conn, run_id)
    assert (now.status, now.done, now.total, now.note, len(judge.seen)) == ("done", 6, 6, "", 6)


async def test_a_stop_of_hunterai_ends_the_judging_as_interrupted(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    held = use(verdict("pass"), name="judge", cls=Held)
    await judging.start(db.connect, run_id, JUDGE)
    await asyncio.wait_for(held.reached.wait(), 20)
    await asyncio.wait_for(judging.shutdown(), 30)
    now = await store.judging(conn, run_id)
    assert (now.status, now.note, now.done) == ("interrupted", "HunterAI stopped while this judging was running.", 0)
    assert judging.running() is None


async def test_a_judging_that_a_stop_of_hunterai_cut_off_is_marked_at_the_next_start(conn):
    run_id = await bench(LOOKUP, says("There is no record."), says("It is 42."))
    await store.start_judging(conn, run_id, JUDGE, 2)
    assert (await store.judging(conn, run_id)).status == "running"
    await judging.recover(conn)
    now = await store.judging(conn, run_id)
    assert (now.status, now.note) == ("interrupted", "HunterAI stopped while this judging was running.")
