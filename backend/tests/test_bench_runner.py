"""A whole bench: every case of the set on every model, one run after another, stored as it goes."""
import asyncio

import pytest

from hunterai import cooling, db, thermal
from hunterai.bench import cases, runner, score
from hunterai.bench.cases import CaseError
from hunterai.providers import registry
from hunterai.providers.base import TextDelta
from hunterai.repo import bench as store
from hunterai.repo.bench import ModelRef
from hunterai.tools import native
from tests.bench_model import TWO, Held, Model, Wordy, calls, fails, says, thinks

A, B = ModelRef(provider="scripted", model="a"), ModelRef(provider="other", model="b")
LOOKUP = calls("cve", action="lookup", ids=["CVE-2026-109618"])


@pytest.fixture(autouse=True)
async def a_bench_of_its_own(isolated_home):
    native.register_all()
    cases.folder().mkdir(parents=True)
    (cases.folder() / "two.toml").write_text(TWO)
    yield
    await asyncio.wait_for(runner.shutdown(), 30)
    assert runner.running() is None


def use(*turns, name="scripted", cls=Model):
    model = cls(*turns)
    registry.override(name, model)
    return model


async def ended(run_id):
    """Waits for a bench to end. A bench that does not is a failure of the test, not a test that never ends."""
    await asyncio.wait_for(runner.wait(run_id), 30)


async def bench(models=(A,), set_id="own:two", repeats=1):
    started = await runner.start(db.connect, list(models), set_id, repeats)
    await ended(started.id)
    return started.id


async def stored(conn, run_id):
    return await store.get(conn, run_id), await store.results(conn, run_id)


# ---- what runs, and in which order

async def test_every_case_of_a_model_runs_before_the_next_model(conn):
    use(says("There is no record. 42."))
    use(says("42, and no record."), name="other")
    run, results = await stored(conn, await bench([A, B], repeats=2))
    assert [(r.provider, r.model, r.case_id, r.repeat) for r in results] == [
        ("scripted", "a", "record", 1), ("scripted", "a", "record", 2), ("scripted", "a", "sum", 1), ("scripted", "a", "sum", 2),
        ("other", "b", "record", 1), ("other", "b", "record", 2), ("other", "b", "sum", 1), ("other", "b", "sum", 2)]
    assert {r.status for r in results} == {"pass"}
    assert (run.status, run.note, run.repeats, run.set_name) == ("done", "", 2, "Two") and run.ended_at
    assert run.models == [A, B] and [c.id for c in run.cases] == ["record", "sum"]


async def test_each_run_is_a_new_conversation_on_the_model_of_its_turn(conn):
    a, b = use(says("ok")), use(says("ok"), name="other")
    await bench([A, B], repeats=2)
    for model, name in ((a, "a"), (b, "b")):
        assert [seen["model"] for seen in model.seen] == [name] * 4
        assert [len(seen["messages"]) for seen in model.seen] == [1] * 4
        assert [seen["messages"][0].blocks[0].text for seen in model.seen] == ["what is CVE-2026-109618?"] * 2 + ["what is 19 + 23?"] * 2


async def test_a_pass_and_a_fail_are_stored_with_their_reasons(conn):
    use(LOOKUP, says("There is no record of it."), calls("encode_decode", operation="md5", text="19+23"), says("It is 41."))
    run, (record, total) = await stored(conn, await bench())
    assert (record.status, record.reasons, record.answer, record.turns) == ("pass", [], "There is no record of it.", 2)
    assert [(c.name, c.problem) for c in record.calls] == [("cve", None)] and record.calls_made == 1
    assert (record.input_tokens, record.output_tokens) == (20, 10) and record.duration_ms >= 0
    assert (total.status, total.answer) == ("fail", "It is 41.")
    assert total.reasons == ["the answer does not say '42'", "made 1 tool call, more than 0"]
    assert run.status == "done"


def test_the_line_under_such_a_run_says_what_was_done_and_that_it_is_no_turn():
    assert score.ASKED_AGAIN == ("a turn ended with thinking only and was asked once more with thinking off, as a chat does it "
                                 "(not counted as a turn)")


async def test_a_run_that_was_asked_again_with_thinking_off_says_so_under_its_mark(conn):
    use(LOOKUP, thinks(), says("There is no record of it."), calls("encode_decode", operation="md5", text="19+23"), says("It is 41."))
    _, (record, total) = await stored(conn, await bench())
    assert (record.status, record.answer, record.turns) == ("pass", "There is no record of it.", 2)
    assert record.reasons == [score.ASKED_AGAIN]
    assert total.reasons == ["the answer does not say '42'", "made 1 tool call, more than 0"]       # thinking is on again for the next run


async def test_a_bench_started_without_thinking_asks_every_call_of_every_run_that_way(conn):
    model = use(calls("cve", action="lookup", ids=["CVE-2026-109618"]), says("There is no record."), says("It is 42."))
    started = await runner.start(db.connect, [A], "own:two", 1, thinking_off=True)
    await ended(started.id)
    assert started.thinking_off is True and (await store.get(conn, started.id)).thinking_off is True
    assert len(model.seen) == 3 and {s["options"].thinking for s in model.seen} == {False}


async def test_a_bench_asks_with_thinking_as_the_model_has_it_when_nothing_else_is_said(conn):
    model = use(says("no record, 42"))
    started = await runner.start(db.connect, [A], "own:two", 1)
    await ended(started.id)
    assert started.thinking_off is False and {s["options"].thinking for s in model.seen} == {None}


async def test_the_time_a_run_took_is_stored(conn):
    class Thinks(Model):
        async def stream(self, model, system, messages, options, tools=None):
            await asyncio.sleep(0.06)
            for ev in self._next(model, system, messages, options, tools):
                yield ev
    use(says("no record, 42"), cls=Thinks)
    _, results = await stored(conn, await bench())
    assert all(50 <= r.duration_ms < 5000 for r in results)


async def test_the_wait_for_a_hot_machine_is_not_in_the_time_that_is_stored(conn, monkeypatch):
    """The time is the model's: a model that ran after a pause for the machine is not the slower one for it."""
    async def slow_to_cool(wait, step, margin, tell):
        await asyncio.sleep(0.6)
    monkeypatch.setattr(cooling, "here", lambda provider, model: True)
    monkeypatch.setattr(thermal, "wait_cool", slow_to_cool)
    use(says("no record, 42"))
    _, results = await stored(conn, await bench())
    assert len(results) == 2 and all(r.duration_ms < 400 for r in results)


async def test_a_run_that_broke_keeps_the_time_until_it_broke(conn):
    class Breaks(Model):
        async def stream(self, model, system, messages, options, tools=None):
            self._next(model, system, messages, options, tools)
            await asyncio.sleep(0.06)
            raise RuntimeError("the adapter broke")
            yield                                   # (a stream is a generator)
    use(cls=Breaks)
    _, results = await stored(conn, await bench())
    assert [r.status for r in results] == ["error", "error"] and all(50 <= r.duration_ms < 5000 for r in results)


async def test_the_number_of_offered_tools_is_the_most_a_model_of_the_bench_was_offered(conn):
    """A model that takes no tools is offered none; the bench says what the others faced."""
    use(says("no record, 42"), cls=Wordy)
    with_tools = use(says("no record, 42"), name="other")
    run, results = await stored(conn, await bench([A, B]))
    assert run.tools_offered == len(with_tools.seen[0]["tools"]) > 20 and len(results) == 4


async def test_the_number_of_offered_tools_is_noted(conn):
    model = use(says("ok"))
    run, _ = await stored(conn, await bench())
    assert run.tools_offered == len(model.seen[0]["tools"]) > 20


async def test_the_same_model_picked_twice_runs_once(conn):
    use(says("ok"))
    run, results = await stored(conn, await bench([A, A, ModelRef(provider="scripted", model="a")]))
    assert run.models == [A] and len(results) == 2


# ---- how far it is

async def test_progress_says_where_the_bench_is(conn):
    held = use(says("no record, 42"), cls=Held)
    use(says("no record, 42"), name="other")
    started = await runner.start(db.connect, [A, B], "own:two", 2)
    assert started.status == "running" and runner.running() == started.id
    await asyncio.wait_for(held.reached.wait(), 5)
    at = {"model": 1, "models": 2, "case": 1, "cases": 2, "repeat": 1, "repeats": 2, "done": 0, "total": 8, "doing": "running"}
    assert runner.progress(started.id) == at
    await held.step()
    assert runner.progress(started.id) == {**at, "repeat": 2, "done": 1}
    await held.step()
    assert runner.progress(started.id) == {**at, "case": 2, "repeat": 1, "done": 2}
    assert runner.progress("another") is None
    held.release()
    await ended(started.id)
    assert runner.progress(started.id) is None and runner.running() is None
    assert len(await store.results(conn, started.id)) == 8


async def test_progress_is_a_copy(conn):
    held = use(says("ok"), cls=Held)
    started = await runner.start(db.connect, [A], "own:two", 1)
    await asyncio.wait_for(held.reached.wait(), 5)
    runner.progress(started.id)["done"] = 99
    assert runner.progress(started.id)["done"] == 0
    held.release()


async def test_progress_says_when_it_waits_for_the_machine(conn, monkeypatch):
    hot = thermal.Part(thermal.GRAPHICS, 88.0, 85.0, "HUNTERAI_GPU_LIMIT")
    waiting, cooled = asyncio.Event(), asyncio.Event()

    async def wait_cool(wait, step, margin, tell):
        if not cooled.is_set():
            await tell(hot)
            waiting.set()
            await cooled.wait()

    monkeypatch.setattr(cooling, "here", lambda provider, model: True)
    monkeypatch.setattr(thermal, "wait_cool", wait_cool)
    held = use(says("ok"), cls=Held)
    started = await runner.start(db.connect, [A], "own:two", 1)
    await asyncio.wait_for(waiting.wait(), 5)
    assert runner.progress(started.id)["doing"] == "cooling"
    cooled.set()
    await asyncio.wait_for(held.reached.wait(), 5)
    assert runner.progress(started.id)["doing"] == "running"
    held.release()


# ---- one bench at a time

async def test_a_second_start_while_one_runs_is_busy(conn):
    held = use(says("ok"), cls=Held)
    started = await runner.start(db.connect, [A], "own:two", 1)
    with pytest.raises(runner.Busy):
        await runner.start(db.connect, [A], "own:two", 1)
    assert [b.id for b in await store.list(conn)] == [started.id]
    held.release()


async def test_of_two_starts_at_the_same_moment_one_is_busy(conn):
    held = use(says("ok"), cls=Held)
    first, second = await asyncio.gather(runner.start(db.connect, [A], "own:two", 1), runner.start(db.connect, [A], "own:two", 1),
                                         return_exceptions=True)
    assert sorted(type(x).__name__ for x in (first, second)) == ["BenchRun", "Busy"]
    assert len(await store.list(conn)) == 1
    held.release()


async def test_a_bench_can_start_after_one_ended(conn):
    use(says("ok"))
    first = await bench()
    second = await bench()
    assert first != second and [b.status for b in await store.list(conn)] == ["done", "done"]


# ---- a model that does not answer

async def test_three_errors_in_a_row_skip_the_rest_of_that_model(conn):
    gone = use(fails("bad_request", "model 'a' not found"))
    fine = use(says("no record, 42"), name="other", cls=Held)
    started = await runner.start(db.connect, [A, B], "own:two", 3)
    await asyncio.wait_for(fine.reached.wait(), 5)
    assert runner.progress(started.id) == {"model": 2, "models": 2, "case": 1, "cases": 2, "repeat": 1, "repeats": 3,
                                           "done": 6, "total": 12, "doing": "running"}       # a skipped run is a run that is done
    fine.release()
    await ended(started.id)
    run, results = await stored(conn, started.id)
    mine, theirs = results[:6], results[6:]
    assert [r.status for r in mine] == ["error"] * 3 + ["skipped"] * 3
    assert mine[0].reasons == ["the model call failed: model 'a' not found"]
    assert all(r.reasons == ["skipped: three runs of this model in a row ended with an error or at the time limit"] for r in mine[3:])
    assert [(r.case_id, r.repeat) for r in mine] == [("record", 1), ("record", 2), ("record", 3), ("sum", 1), ("sum", 2), ("sum", 3)]
    assert len(gone.seen) == 3                                   # no call was made for a run that was skipped
    assert [r.status for r in theirs] == ["pass"] * 6 and len(fine.seen) == 6
    assert run.status == "done"


async def test_an_error_between_passes_skips_nothing(conn):
    use(fails("timeout", "no answer"), fails("timeout", "no answer"), says("no record, 42"),
        fails("timeout", "no answer"), fails("timeout", "no answer"), says("no record, 42"))
    _, results = await stored(conn, await bench(repeats=3))
    assert [r.status for r in results] == ["error", "error", "pass", "error", "error", "pass"]


async def test_the_count_of_errors_starts_again_with_the_next_model(conn):
    use(says("no record, 42"), says("no record, 42"), fails("timeout", "no answer"))
    use(fails("timeout", "no answer"), fails("timeout", "no answer"), says("no record, 42"), name="other")
    _, results = await stored(conn, await bench([A, B], repeats=2))
    assert [r.status for r in results] == ["pass", "pass", "error", "error", "error", "error", "pass", "pass"]


async def test_runs_that_end_at_the_time_limit_count_like_errors(conn, monkeypatch):
    monkeypatch.setattr(score, "TIME_LIMIT", 0.05)

    class Endless(Model):
        async def stream(self, model, system, messages, options, tools=None):
            self._next(model, system, messages, options, tools)
            yield TextDelta("and so on")
            await asyncio.sleep(30)

    model = use(cls=Endless)
    _, results = await stored(conn, await asyncio.wait_for(bench(repeats=2), 20))
    assert [r.status for r in results] == ["fail", "fail", "fail", "skipped"]
    assert results[0].reasons[0].startswith("did not answer within ") and len(model.seen) == 3


async def test_a_failure_in_one_run_is_the_error_of_that_run_and_the_bench_goes_on(conn):
    use([RuntimeError("the adapter broke")], says("no record, 42"))
    run, (broken, fine) = await stored(conn, await bench())
    assert broken.status == "error" and fine.status == "pass" and run.status == "done"
    [reason] = broken.reasons
    assert reason.startswith("HunterAI failed while running this test (RuntimeError: the adapter broke).")


# ---- a bench that ends early

async def test_a_hot_machine_stops_the_bench_with_the_reason(conn, monkeypatch):
    hot = thermal.Part(thermal.PROCESSOR, 97.0, 90.0, "HUNTERAI_CPU_LIMIT")
    looks = 0

    async def wait_cool(wait, step, margin, tell):
        nonlocal looks
        looks += 1
        if looks < 2:
            return None
        await tell(hot)
        return hot

    monkeypatch.setattr(cooling, "here", lambda provider, model: True)
    monkeypatch.setattr(thermal, "wait_cool", wait_cool)
    model = use(says("no record, 42"))
    use(says("no record, 42"), name="other")
    run, results = await stored(conn, await bench([A, B]))
    assert run.status == "stopped" and run.ended_at
    assert run.note == ("The processor is at 97 °C and did not cool to 85 °C within 3 minutes (its limit is 90 °C). The bench was "
                        "stopped before the local model a was called. Let the machine cool and start the bench again. "
                        "HUNTERAI_CPU_LIMIT sets the limit in °C (off: no limit).")
    assert [(r.case_id, r.status) for r in results] == [("record", "pass")] and len(model.seen) == 1


async def test_cancel_keeps_what_was_stored(conn):
    held = use(says("no record, 42"), cls=Held)
    started = await runner.start(db.connect, [A], "own:two", 2)
    await asyncio.wait_for(held.reached.wait(), 5)
    await held.step()
    assert await runner.cancel("another") is False
    assert await runner.cancel(started.id) is True
    await ended(started.id)
    run, results = await stored(conn, started.id)
    assert (run.status, run.note) == ("cancelled", "") and run.ended_at
    assert [(r.case_id, r.repeat, r.status) for r in results] == [("record", 1, "pass")]
    assert runner.running() is None and await runner.cancel(started.id) is False


async def test_a_cancel_before_the_first_step_still_ends_the_bench(conn):
    use(says("ok"))
    started = await runner.start(db.connect, [A], "own:two", 1)
    assert await runner.cancel(started.id) is True          # the task has not run a line yet
    await ended(started.id)
    run, results = await stored(conn, started.id)
    assert (run.status, results) == ("cancelled", []) and runner.running() is None
    assert (await stored(conn, await bench()))[0].status == "done"


async def test_stopping_hunterai_ends_a_running_bench_as_interrupted(conn):
    held = use(says("no record, 42"), cls=Held)
    started = await runner.start(db.connect, [A], "own:two", 2)
    await asyncio.wait_for(held.reached.wait(), 5)
    await held.step()
    await runner.shutdown()
    run, results = await stored(conn, started.id)
    assert (run.status, run.note) == ("interrupted", "HunterAI stopped while this bench was running.")
    assert len(results) == 1 and runner.running() is None
    await runner.shutdown()         # and with nothing running it does nothing


async def test_a_failure_of_the_bench_itself_is_told(conn, monkeypatch):
    use(says("no record, 42"))

    async def broken(*a, **k):
        raise RuntimeError("the disk is full")
    monkeypatch.setattr(store, "add_result", broken)
    run, results = await stored(conn, await bench())
    assert run.status == "stopped" and results == []
    assert run.note.startswith("HunterAI failed while running the bench (RuntimeError: the disk is full).")


async def test_recover_marks_a_bench_from_before_as_interrupted(conn):
    left = await store.create(conn, cases.load("own:two"), [A], 1)
    await runner.recover(conn)
    now = await store.get(conn, left.id)
    assert (now.status, now.note) == ("interrupted", "HunterAI stopped while this bench was running.")
    use(says("ok"))
    assert (await stored(conn, await bench()))[0].status == "done"


# ---- what a start refuses

async def test_start_refuses_no_models_wrong_repeats_and_unknown_providers(conn):
    use(says("ok"))
    for models, repeats, said in (([], 1, "pick at least one model"), ([A], 0, "repeats is 1 to 5"), ([A], 6, "repeats is 1 to 5"),
                                  ([A, ModelRef(provider="nobody", model="x")], 1, "unknown provider nobody"),
                                  ([ModelRef(provider="scripted", model=str(n)) for n in range(21)], 1, "pick at most 20 models")):
        with pytest.raises(ValueError) as e:
            await runner.start(db.connect, models, "own:two", repeats)
        assert str(e.value) == said and not isinstance(e.value, CaseError)
    with pytest.raises(KeyError):
        await runner.start(db.connect, [A], "own:nothing", 1)
    (cases.folder() / "broken.toml").write_text("[[case]]\n")
    with pytest.raises(CaseError) as e:
        await runner.start(db.connect, [A], "own:broken", 1)
    assert str(e.value) == "case 1: it has no id"
    assert await store.list(conn) == [] and runner.running() is None
    assert (await stored(conn, await bench(repeats=5)))[0].status == "done"       # five repeats and a start after refusals


async def test_twenty_models_can_be_picked(conn):
    for n in range(20):
        use(says("no record, 42"), name=f"p{n}")
    run, results = await stored(conn, await bench([ModelRef(provider=f"p{n}", model="m") for n in range(20)]))
    assert run.status == "done" and len(results) == 40


async def test_a_set_that_names_a_tool_hunterai_does_not_have_says_so(conn):
    (cases.folder() / "typo.toml").write_text(TWO.replace('name = "cve"', 'name = ["cve", "web_serach"]')
                                              + '\n[[case]]\nid = "third"\ngroup = "tool-use"\nprompt = "?"\n'
                                                '[case.expect]\ncalls_any = ["nmap_scan", "cve"]\ncalls_none = ["exceute"]\n')
    held = use(says("no record, 42"), cls=Held)
    started = await runner.start(db.connect, [A], "own:typo", 1)
    said = ("This set names tools HunterAI does not have at the moment: exceute, nmap_scan, web_serach. "
            "A test that needs one of them cannot pass.")
    assert started.note == said and (await store.get(conn, started.id)).note == said
    await asyncio.wait_for(held.reached.wait(), 5)
    await runner.cancel(started.id)
    await ended(started.id)
    assert (await store.get(conn, started.id)).note == said        # the end of a bench keeps it


async def test_the_note_names_ten_unknown_tools_and_counts_the_rest(conn):
    names = [f"tool_{n:02}" for n in range(11)]
    (cases.folder() / "many.toml").write_text('[[case]]\nid = "one"\ngroup = "tool-use"\nprompt = "?"\n[case.expect]\n'
                                              f'calls_any = {names!r}\n'.replace("'", '"'))
    use(says("ok"))
    started = await runner.start(db.connect, [A], "own:many", 1)
    assert started.note == ("This set names tools HunterAI does not have at the moment: " + ", ".join(names[:10])
                            + " and 1 more. A test that needs one of them cannot pass.")
    (cases.folder() / "ten.toml").write_text('[[case]]\nid = "one"\ngroup = "tool-use"\nprompt = "?"\n[case.expect]\n'
                                             f'calls_any = {names[:10]!r}\n'.replace("'", '"'))
    await ended(started.id)
    again = await runner.start(db.connect, [A], "own:ten", 1)
    assert again.note.endswith("tool_08, tool_09. A test that needs one of them cannot pass.")


async def test_a_set_whose_tools_hunterai_has_starts_without_a_note(conn):
    use(says("ok"))
    assert (await runner.start(db.connect, [A], "own:two", 1)).note == ""


async def test_the_note_about_tools_stands_before_the_reason_a_bench_stopped(conn, monkeypatch):
    (cases.folder() / "typo.toml").write_text(TWO.replace('name = "cve"', 'name = "web_serach"'))

    async def broken(*a, **k):
        raise RuntimeError("the disk is full")
    monkeypatch.setattr(store, "add_result", broken)
    use(says("ok"))
    run, _ = await stored(conn, await bench(set_id="own:typo"))
    assert run.note.startswith("This set names tools HunterAI does not have at the moment: web_serach. A test that needs one of them "
                               "cannot pass. HunterAI failed while running the bench (RuntimeError: the disk is full).")


# ---- the window of a local model, and what is left alone

class Once(Model):
    """A provider that sets the window of each call itself and offers a larger one the first time it is asked only."""
    offered = 0

    async def window_for(self, model, options):
        return options.context_window or 2048

    async def larger_windows(self, model, options):
        self.offered += 1
        return [300_000] if self.offered == 1 else []


async def test_the_window_a_model_was_raised_to_is_kept_for_its_next_cases_and_not_for_the_next_model(conn):
    a, b = use(says("no record, 42"), cls=Once), use(says("no record, 42"), name="other")
    await bench([A, B], repeats=2)
    assert [seen["options"].context_window for seen in a.seen] == [300_000] * 4
    assert [seen["options"].context_window for seen in b.seen] == [None] * 4


async def test_a_bench_leaves_the_chat_tables_empty(conn):
    use(LOOKUP, says("no record"), says("42"))
    await bench()
    for table in ("chats", "messages", "runs", "steps", "targets"):
        assert (await (await conn.execute(f"select count(*) from {table}")).fetchone())[0] == 0
    assert not (cases.folder() / "work").exists()
