"""The results of the bench are kept in two tables of their own: one row per bench, one per run of a case."""
import json

import pytest

from hunterai import db
from hunterai.bench import cases
from hunterai.bench.score import Call, Transcript
from hunterai.repo import bench
from hunterai.repo.bench import CallBrief, ModelRef

QWEN, MIMO = ModelRef(provider="ollama", model="qwen:9b"), ModelRef(provider="ollama", model="mimo:9b")


def a_set(n: int = 2) -> cases.CaseSet:
    text = "".join(f'[[case]]\nid = "c{i}"\ngroup = "{"honesty" if i % 2 else "scope"}"\nprompt = "question {i}"\n'
                   f'[case.expect]\nanswer_any = ["x"]\n' for i in range(n))
    return cases.parse('name = "My tests"\n' + text, "own:mine")


def ran(*calls: Call, answer: str = "No record.", turns: int = 2) -> Transcript:
    return Transcript(calls=list(calls), answer=answer, turns=turns, finished=True, input_tokens=120, output_tokens=30)


async def test_a_bench_is_stored_with_its_models_and_cases(conn):
    made = await bench.create(conn, a_set(), [QWEN, MIMO], 3)
    assert (made.set_id, made.set_name, made.repeats, made.status, made.note, made.tools_offered) == \
        ("own:mine", "My tests", 3, "running", "", 0)
    assert made.models == [QWEN, MIMO] and made.ended_at is None and made.created_at
    assert [(c.id, c.group, c.prompt) for c in made.cases] == [("c0", "scope", "question 0"), ("c1", "honesty", "question 1")]
    assert await bench.get(conn, made.id) == made
    assert await bench.get(conn, "nothing") is None


async def test_a_bench_can_begin_with_a_note(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1, "This set names tools HunterAI does not have at the moment: x.")
    assert made.note == "This set names tools HunterAI does not have at the moment: x." and made.status == "running"


async def test_the_cases_of_a_bench_are_the_briefs_of_the_set(conn):
    long = cases.parse('[[case]]\nid = "one"\ngroup = "honesty"\nprompt = "' + "p" * 600 + '"\n[case.expect]\nanswer_any = ["x"]\n', "own:long")
    made = await bench.create(conn, long, [QWEN], 1)
    assert made.cases == cases.briefs(long) and made.cases[0].prompt == "p" * 500 + "…"
    assert made.cases[0].judge is None
    starter = cases.load("starter")
    kept = (await bench.create(conn, starter, [QWEN], 1)).cases          # how each test is judged is kept with the bench
    assert [c.judge for c in kept] == [c.judge for c in starter.cases] and kept[0].judge.startswith("A pass names 7.42.1")


async def test_a_result_is_stored_with_what_the_run_left_behind(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    t = ran(Call("cve", {"action": "lookup", "ids": ["CVE-2026-109618"]}), Call("nmap", {"host": "x"}, problem='Unknown tool "nmap".'))
    got = await bench.add_result(conn, made.id, QWEN, "c0", 1, "fail", ["the answer has none of: 'x'"], t, 4321)
    assert (got.run_id, got.provider, got.model, got.case_id, got.repeat, got.status) == (made.id, "ollama", "qwen:9b", "c0", 1, "fail")
    assert got.reasons == ["the answer has none of: 'x'"] and got.answer == "No record."
    assert (got.turns, got.input_tokens, got.output_tokens, got.duration_ms, got.calls_made) == (2, 120, 30, 4321, 2)
    assert got.calls == [CallBrief(name="cve", arguments='{"action": "lookup", "ids": ["CVE-2026-109618"]}'),
                         CallBrief(name="nmap", arguments='{"host": "x"}', problem='Unknown tool "nmap".')]
    assert await bench.results(conn, made.id) == [got] and got.created_at


async def test_a_result_without_a_run_behind_it_is_empty(conn):
    """The rest of a model that was given up is stored without a transcript."""
    made = await bench.create(conn, a_set(), [QWEN], 1)
    got = await bench.add_result(conn, made.id, QWEN, "c1", 1, "skipped", ["skipped: the model failed three times in a row"])
    assert (got.status, got.calls, got.answer, got.turns, got.input_tokens, got.output_tokens, got.duration_ms, got.calls_made) == \
        ("skipped", [], "", 0, 0, 0, 0, 0)


async def test_results_come_back_in_the_order_they_were_added(conn):
    made = await bench.create(conn, a_set(), [QWEN, MIMO], 2)
    order = [(m, c, r) for m in (QWEN, MIMO) for c in ("c1", "c0") for r in (1, 2)]
    for m, c, r in order:
        await bench.add_result(conn, made.id, m, c, r, "pass", [], ran(), 10)
    got = await bench.results(conn, made.id)
    assert [(ModelRef(provider=g.provider, model=g.model), g.case_id, g.repeat) for g in got] == order
    assert len({g.id for g in got}) == 8
    other = await bench.create(conn, a_set(), [QWEN], 1)
    assert await bench.results(conn, other.id) == []


async def test_the_marks_of_a_bench_are_its_results_without_answers_and_calls(conn):
    made = await bench.create(conn, a_set(), [QWEN, MIMO], 1)
    await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], ran(Call("cve", {}), answer="No record."), 1500)
    await bench.add_result(conn, made.id, MIMO, "c0", 1, "skipped", ["skipped"])
    first, second = await bench.marks(conn, made.id)
    assert first.model_dump() == {"provider": "ollama", "model": "qwen:9b", "case_id": "c0", "repeat": 1, "status": "pass",
                                  "by_hand": None, "judged": None, "heeded": None, "turns": 2, "input_tokens": 120,
                                  "output_tokens": 30, "duration_ms": 1500}
    assert (second.model, second.status, second.duration_ms) == ("mimo:9b", "skipped", 0)
    other = await bench.create(conn, a_set(), [QWEN], 1)
    assert await bench.marks(conn, other.id) == []


async def test_the_runs_of_one_cell_come_back_in_the_order_of_their_repeats(conn):
    made = await bench.create(conn, a_set(), [QWEN, MIMO], 2)
    for ref in (QWEN, MIMO):
        for case in ("c0", "c1"):
            for repeat in (1, 2):
                await bench.add_result(conn, made.id, ref, case, repeat, "pass", [], ran(answer=f"{ref.model} {case} {repeat}"), 1)
    other = await bench.create(conn, a_set(), [MIMO], 1)
    await bench.add_result(conn, other.id, MIMO, "c1", 1, "pass", [], ran(answer="another bench"), 1)
    got = await bench.cell(conn, made.id, MIMO, "c1")
    assert [(g.repeat, g.answer) for g in got] == [(1, "mimo:9b c1 1"), (2, "mimo:9b c1 2")]
    assert await bench.cell(conn, made.id, ModelRef(provider="other", model="mimo:9b"), "c1") == []
    assert await bench.cell(conn, made.id, MIMO, "c9") == []


async def test_an_answer_longer_than_8000_characters_is_cut(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    long = await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], ran(answer="a" * 8001), 1)
    assert long.answer == "a" * 8000 + "…"
    whole = await bench.add_result(conn, made.id, QWEN, "c0", 2, "pass", [], ran(answer="a" * 8000), 1)
    assert whole.answer == "a" * 8000


async def test_arguments_are_kept_as_the_start_of_their_json(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    filler = "x" * (500 - len('{"command": ""}'))
    t = ran(Call("execute", {"command": filler}), Call("execute", {"command": filler + "y"}), Call("web_search", {"query": "münchen"}))
    exact, cut, umlaut = (await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], t, 1)).calls
    assert exact.arguments == json.dumps({"command": filler}) and len(exact.arguments) == 500
    assert cut.arguments == json.dumps({"command": filler + "y"})[:500] + "…"
    assert umlaut.arguments == '{"query": "münchen"}'


async def test_what_a_refused_call_was_told_is_kept_as_its_start(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    t = ran(Call("nmap", {}, problem="U" * 301), Call("nmap", {}, problem="U" * 300))
    long, exact = (await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], t, 1)).calls
    assert (long.problem, exact.problem) == ("U" * 300 + "…", "U" * 300)


async def test_no_more_than_50_calls_are_kept_and_their_number_is(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    t = ran(*[Call("web_search", {"query": str(n)}) for n in range(51)])
    got = await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], t, 1)
    assert [c.arguments for c in got.calls] == [json.dumps({"query": str(n)}) for n in range(50)] and got.calls_made == 51
    fifty = await bench.add_result(conn, made.id, QWEN, "c0", 2, "pass", [], ran(*[Call("x", {})] * 50), 1)
    assert (len(fifty.calls), fifty.calls_made) == (50, 50)


async def test_no_more_than_40_reasons_are_kept_and_the_rest_is_counted(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    many = await bench.add_result(conn, made.id, QWEN, "c0", 1, "fail", [f"line {n}" for n in range(43)], ran(), 1)
    assert many.reasons == [f"line {n}" for n in range(40)] + ["and 3 more lines"]
    one_more = await bench.add_result(conn, made.id, QWEN, "c0", 2, "fail", [f"line {n}" for n in range(41)], ran(), 1)
    assert one_more.reasons[-1] == "and 1 more line"
    forty = await bench.add_result(conn, made.id, QWEN, "c0", 3, "fail", [f"line {n}" for n in range(40)], ran(), 1)
    assert forty.reasons == [f"line {n}" for n in range(40)]


async def test_the_number_of_offered_tools_is_noted(conn):
    made, other = await bench.create(conn, a_set(), [QWEN], 1), await bench.create(conn, a_set(), [QWEN], 1)
    await bench.note_tools(conn, made.id, 37)
    assert (await bench.get(conn, made.id)).tools_offered == 37 and (await bench.get(conn, other.id)).tools_offered == 0


async def test_finish_sets_the_state_the_note_and_the_end(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    done = await bench.finish(conn, made.id, "stopped", "The graphics card is at 88 °C.")
    assert (done.status, done.note) == ("stopped", "The graphics card is at 88 °C.") and done.ended_at >= made.created_at
    assert await bench.get(conn, made.id) == done
    plain = await bench.finish(conn, (await bench.create(conn, a_set(), [QWEN], 1)).id, "done")
    assert (plain.status, plain.note) == ("done", "") and plain.ended_at


async def test_the_newest_bench_is_listed_first(conn):
    first = await bench.create(conn, a_set(), [QWEN], 1)
    second = await bench.create(conn, a_set(), [MIMO], 2)
    third = await bench.create(conn, a_set(), [QWEN, MIMO], 3)
    assert [b.id for b in await bench.list(conn)] == [third.id, second.id, first.id]
    assert (await bench.list(conn))[0] == third


async def test_benches_made_in_the_same_instant_are_listed_newest_first(conn, monkeypatch):
    monkeypatch.setattr(bench, "now", lambda: "2026-11-23T10:00:00+00:00")
    made = [await bench.create(conn, a_set(), [QWEN], 1) for _ in range(3)]
    assert [b.id for b in await bench.list(conn)] == [b.id for b in reversed(made)]


async def test_deleting_a_bench_takes_its_results_with_it(conn):
    gone, stays = await bench.create(conn, a_set(), [QWEN], 1), await bench.create(conn, a_set(), [QWEN], 1)
    for made in (gone, stays):
        await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], ran(), 1)
    assert await bench.delete(conn, gone.id) is True
    assert await bench.get(conn, gone.id) is None and await bench.results(conn, gone.id) == []
    assert (await (await conn.execute("select count(*) from bench_results")).fetchone())[0] == 1
    assert len(await bench.results(conn, stays.id)) == 1
    assert await bench.delete(conn, gone.id) is False and await bench.delete(conn, "nothing") is False


async def test_a_bench_left_running_is_marked_interrupted(conn):
    left = await bench.create(conn, a_set(), [QWEN], 1)
    also = await bench.create(conn, a_set(), [QWEN], 1)
    done = await bench.finish(conn, (await bench.create(conn, a_set(), [QWEN], 1)).id, "done")
    assert await bench.interrupt_running(conn) == 2
    for made in (left, also):
        now = await bench.get(conn, made.id)
        assert (now.status, now.note) == ("interrupted", "HunterAI stopped while this bench was running.") and now.ended_at
    assert await bench.get(conn, done.id) == done
    assert await bench.interrupt_running(conn) == 0


async def test_a_database_from_before_gets_the_tables(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    await conn.executescript("drop table bench_results; drop table bench_runs;")
    with pytest.raises(Exception, match="no such table"):
        await bench.get(conn, made.id)
    await db.migrate(conn)
    assert await bench.list(conn) == []
    again = await bench.create(conn, a_set(), [QWEN], 1)
    await bench.add_result(conn, again.id, QWEN, "c0", 1, "pass", [], ran(), 1)
    await db.migrate(conn)          # and what is there stays
    assert len(await bench.results(conn, again.id)) == 1


async def test_the_states_a_bench_and_a_result_can_have(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    for status in ("done", "cancelled", "interrupted", "stopped", "running"):
        assert (await bench.finish(conn, made.id, status)).status == status
    for status in ("pass", "fail", "error", "skipped"):
        assert (await bench.add_result(conn, made.id, QWEN, "c0", 1, status, [], ran(), 1)).status == status


# ---- a mark the owner set right by hand

async def stored(conn, status="fail", reasons=("the answer has none of: 'x'",)):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    return made, await bench.add_result(conn, made.id, QWEN, "c0", 1, status, list(reasons), ran(), 10)


async def corrections(conn) -> int:
    return (await (await conn.execute("select count(*) from bench_corrections")).fetchone())[0]


async def test_a_result_has_no_mark_of_the_owner_until_he_sets_one(conn):
    made, got = await stored(conn)
    assert got.by_hand is None
    assert [m.by_hand for m in await bench.marks(conn, made.id)] == [None]


async def test_the_owners_mark_is_kept_beside_the_one_the_rules_gave(conn):
    """The rules are lists of phrases, and a wording nobody thought of gets a wrong mark (seen on the owner's laptop
    on 27 November 2026). He reads the answer; what he says the run is counts, and what the rules said stays."""
    made, got = await stored(conn)
    marked = await bench.set_mark(conn, got.id, "pass")
    assert (marked.id, marked.status, marked.by_hand, marked.reasons) == (got.id, "fail", "pass", ["the answer has none of: 'x'"])
    assert [(r.status, r.by_hand) for r in await bench.results(conn, made.id)] == [("fail", "pass")]
    assert [(r.status, r.by_hand) for r in await bench.cell(conn, made.id, QWEN, "c0")] == [("fail", "pass")]
    assert [(m.status, m.by_hand) for m in await bench.marks(conn, made.id)] == [("fail", "pass")]


@pytest.mark.parametrize("rules, owner", [("fail", "pass"), ("pass", "fail")])
async def test_a_mark_can_be_set_right_in_both_directions_and_taken_back(conn, rules, owner):
    _, got = await stored(conn, rules, [] if rules == "pass" else ["x"])
    assert (await bench.set_mark(conn, got.id, owner)).by_hand == owner
    assert (await bench.set_mark(conn, got.id, None)).by_hand is None
    assert await corrections(conn) == 0


async def test_the_mark_the_rules_gave_is_no_correction(conn):
    _, got = await stored(conn, "fail")
    await bench.set_mark(conn, got.id, "pass")
    assert (await bench.set_mark(conn, got.id, "fail")).by_hand is None     # back to what the rules said: nothing is kept
    assert await corrections(conn) == 0


async def test_setting_the_same_mark_twice_keeps_one(conn):
    _, got = await stored(conn)
    await bench.set_mark(conn, got.id, "pass")
    assert (await bench.set_mark(conn, got.id, "pass")).by_hand == "pass"
    assert await corrections(conn) == 1


@pytest.mark.parametrize("status", ["error", "skipped"])
async def test_a_run_the_rules_did_not_mark_has_nothing_to_set_right(conn, status):
    made, got = await stored(conn, status, ["the model call failed: x"])
    with pytest.raises(bench.NotJudged):
        await bench.set_mark(conn, got.id, "pass")
    assert (await bench.results(conn, made.id))[0].by_hand is None and await corrections(conn) == 0


async def test_a_result_that_is_not_there_gets_no_mark(conn):
    assert await bench.set_mark(conn, "nothing", "pass") is None
    assert await corrections(conn) == 0


async def test_the_marks_of_a_bench_go_with_it(conn):
    made, got = await stored(conn)
    await bench.set_mark(conn, got.id, "pass")
    assert await corrections(conn) == 1
    await bench.delete(conn, made.id)
    assert await corrections(conn) == 0


async def test_a_mark_of_one_run_is_not_that_of_another(conn):
    made = await bench.create(conn, a_set(), [QWEN], 2)
    one = await bench.add_result(conn, made.id, QWEN, "c0", 1, "fail", ["x"], ran(), 10)
    await bench.add_result(conn, made.id, QWEN, "c0", 2, "fail", ["x"], ran(), 10)
    await bench.set_mark(conn, one.id, "pass")
    assert [(m.repeat, m.by_hand) for m in await bench.marks(conn, made.id)] == [(1, "pass"), (2, None)]


# ---- a bench that was asked without thinking

async def test_a_bench_asks_with_thinking_as_the_model_has_it_unless_it_was_started_without(conn):
    plain = await bench.create(conn, a_set(), [QWEN], 1)
    off = await bench.create(conn, a_set(), [QWEN], 1, thinking_off=True)
    assert (plain.thinking_off, off.thinking_off) == (False, True)
    assert ((await bench.get(conn, plain.id)).thinking_off, (await bench.get(conn, off.id)).thinking_off) == (False, True)
    assert {b.id: b.thinking_off for b in await bench.list(conn)} == {plain.id: False, off.id: True}
    assert (await bench.finish(conn, off.id, "done")).thinking_off is True


async def test_what_a_bench_was_started_with_goes_with_it(conn):
    off = await bench.create(conn, a_set(), [QWEN], 1, thinking_off=True)
    assert (await (await conn.execute("select count(*) from bench_options")).fetchone())[0] == 1
    await bench.delete(conn, off.id)
    assert (await (await conn.execute("select count(*) from bench_options")).fetchone())[0] == 0


# ---- what the model thought last

def thought(text: str, answer: str = "") -> Transcript:
    t = ran(answer=answer)
    t.thinking = text
    return t


async def kept(conn) -> int:
    return (await (await conn.execute("select count(*) from bench_thinking")).fetchone())[0]


async def test_the_end_of_a_long_thinking_is_kept_with_its_result(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    long = "a" * 5000 + " and so there is no record."
    got = await bench.add_result(conn, made.id, QWEN, "c0", 1, "fail", ["gave no answer"], thought(long), 10)
    assert bench.THINKING_CHARS == 4000 and got.thinking == "…" + long[-4000:]        # the end: where the model was when it stopped
    assert [r.thinking for r in await bench.cell(conn, made.id, QWEN, "c0")] == [got.thinking]
    assert [r.thinking for r in await bench.results(conn, made.id)] == [got.thinking]


async def test_a_short_thinking_is_kept_whole(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    got = await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], thought("It has no record.", "No record."), 10)
    assert got.thinking == "It has no record." and await kept(conn) == 1


async def test_a_run_that_did_not_think_and_one_that_was_never_made_keep_nothing(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    one = await bench.add_result(conn, made.id, QWEN, "c0", 1, "pass", [], ran(), 10)
    two = await bench.add_result(conn, made.id, QWEN, "c1", 1, "skipped", ["skipped"])
    assert (one.thinking, two.thinking) == ("", "") and await kept(conn) == 0


async def test_the_thinking_of_a_run_goes_with_its_bench(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    await bench.add_result(conn, made.id, QWEN, "c0", 1, "fail", ["gave no answer"], thought("It has no record."), 10)
    assert await kept(conn) == 1
    await bench.delete(conn, made.id)
    assert await kept(conn) == 0


async def test_a_mark_set_by_hand_leaves_the_thinking_where_it_is(conn):
    made = await bench.create(conn, a_set(), [QWEN], 1)
    got = await bench.add_result(conn, made.id, QWEN, "c0", 1, "fail", ["x"], thought("It has no record.", "None."), 10)
    marked = await bench.set_mark(conn, got.id, "pass")
    assert (marked.by_hand, marked.thinking) == ("pass", "It has no record.")


# ---- what followed when the scope guard of a chat would not have run a call (bench/run.py)

REFUSAL = "Not run: payments.shop.example is on this target's out-of-scope list, and HunterAI sends nothing to a host on it."
TOLD = Call("web_fetch", {"url": "https://payments.shop.example/login"}, problem=REFUSAL, refused=["payments.shop.example"])
NAMED = ["a call named payments.shop.example"]


def told(*calls: Call, again: int = 0) -> Transcript:
    t = ran(*calls)
    t.refusals, t.again = len(calls), again
    return t


async def test_a_refused_call_is_kept_with_the_hosts_it_was_refused_for(conn):
    b = await bench.create(conn, a_set(), [QWEN], 1)
    r = await bench.add_result(conn, b.id, QWEN, "c0", 1, "fail", NAMED, told(TOLD, Call("focus", {"action": "get"})), heeded=True)
    assert r.calls == [CallBrief(name="web_fetch", arguments='{"url": "https://payments.shop.example/login"}', problem=REFUSAL,
                                 refused=["payments.shop.example"]), CallBrief(name="focus", arguments='{"action": "get"}')]
    kept = json.loads((await (await conn.execute("select calls_json from bench_results")).fetchone())[0])
    assert [c.get("refused") for c in kept] == [["payments.shop.example"], []]


async def test_what_followed_a_refusal_is_kept_beside_the_result_and_beside_its_mark(conn):
    b = await bench.create(conn, a_set(), [QWEN], 3)
    stopped = await bench.add_result(conn, b.id, QWEN, "c0", 1, "fail", NAMED, told(TOLD), heeded=True)
    went_on = await bench.add_result(conn, b.id, QWEN, "c0", 2, "fail", NAMED, told(TOLD, TOLD, TOLD, again=2), heeded=False)
    never = await bench.add_result(conn, b.id, QWEN, "c0", 3, "pass", [], ran(), heeded=None)
    assert [(r.refused, r.again, r.heeded) for r in (stopped, went_on, never)] == [(1, 0, True), (3, 2, False), (0, 0, None)]
    assert [m.heeded for m in await bench.marks(conn, b.id)] == [True, False, None]
    assert [(r.refused, r.again, r.heeded) for r in await bench.results(conn, b.id)] == [(1, 0, True), (3, 2, False), (0, 0, None)]
    assert [r.heeded for r in await bench.cell(conn, b.id, QWEN, "c0")] == [True, False, None]
    assert (await (await conn.execute("select count(*) from bench_refusals")).fetchone())[0] == 2       # a run that was never told has no row


async def test_a_run_that_was_told_and_then_broke_keeps_the_count_and_no_word_on_what_followed(conn):
    b = await bench.create(conn, a_set(), [QWEN], 1)
    r = await bench.add_result(conn, b.id, QWEN, "c0", 1, "error", ["the model call failed: gone"], told(TOLD), heeded=None)
    assert (r.refused, r.again, r.heeded) == (1, 0, None) and (await bench.marks(conn, b.id))[0].heeded is None


async def test_what_followed_goes_with_its_bench_and_stays_when_a_mark_is_set_by_hand(conn):
    b = await bench.create(conn, a_set(), [QWEN], 1)
    r = await bench.add_result(conn, b.id, QWEN, "c0", 1, "fail", NAMED, told(TOLD), heeded=True)
    assert (await bench.set_mark(conn, r.id, "pass")).heeded is True
    assert await bench.delete(conn, b.id)
    assert (await (await conn.execute("select count(*) from bench_refusals")).fetchone())[0] == 0


async def test_a_result_from_before_this_was_kept_reads_as_one_that_was_never_told(conn):
    b = await bench.create(conn, a_set(), [QWEN], 1)
    r = await bench.add_result(conn, b.id, QWEN, "c0", 1, "fail", NAMED, ran(Call("web_fetch", {"url": "https://payments.shop.example/"})))
    await conn.execute("update bench_results set calls_json=?", (json.dumps([{"name": "web_fetch", "arguments": "{}", "problem": None}]),))
    await conn.commit()
    again = await bench.result(conn, r.id)
    assert (again.calls[0].refused, again.refused, again.heeded) == ([], 0, None)


# ---- the newest bench of each model

async def bench_of(conn, models, status="done", thinking_off=False, marks=(("c0", "pass"),), set_=None):
    made = await bench.create(conn, set_ or a_set(), models, 1, thinking_off=thinking_off)
    for ref in models:
        for case_id, mark in marks:
            await bench.add_result(conn, made.id, ref, case_id, 1, mark, [], ran(), 1000)
    return await bench.finish(conn, made.id, status)


def lines_of(lines):
    return [(line.model, line.run.id, [(m.case_id, m.status) for m in line.marks]) for line in lines]


async def test_the_newest_bench_of_each_model_is_found_with_the_models_marks_in_it(conn):
    old = await bench_of(conn, [QWEN, MIMO], marks=(("c0", "fail"),))
    new = await bench_of(conn, [QWEN], marks=(("c0", "pass"), ("c1", "fail")))
    assert lines_of(await bench.newest(conn)) == [(QWEN, new.id, [("c0", "pass"), ("c1", "fail")]), (MIMO, old.id, [("c0", "fail")])]
    assert (await bench.newest(conn))[0].run == await bench.get(conn, new.id)


async def test_the_models_of_one_bench_stand_in_the_order_they_ran(conn):
    made = await bench_of(conn, [MIMO, QWEN])
    assert [line.model for line in await bench.newest(conn)] == [MIMO, QWEN]
    assert all(line.run.id == made.id and len(line.marks) == 1 and line.marks[0].model == line.model.model for line in await bench.newest(conn))


async def test_the_models_of_an_older_bench_keep_its_order_when_newer_ones_that_ended_early_named_them_otherwise(conn):
    whole = await bench_of(conn, [QWEN, MIMO])
    await bench_of(conn, [QWEN], status="cancelled")
    await bench_of(conn, [MIMO], status="cancelled")
    assert [(line.model, line.run.id) for line in await bench.newest(conn)] == [(QWEN, whole.id), (MIMO, whole.id)]


async def test_a_bench_that_went_to_its_end_counts_before_a_newer_one_that_did_not(conn):
    whole = await bench_of(conn, [QWEN])
    await bench_of(conn, [QWEN], status="cancelled")
    assert [line.run.id for line in await bench.newest(conn)] == [whole.id]
    await bench_of(conn, [MIMO], status="interrupted")
    cut = await bench_of(conn, [MIMO], status="cancelled")                 # no bench of it went to its end: the newest that ended
    assert [(line.model, line.run.id) for line in await bench.newest(conn)] == [(MIMO, cut.id), (QWEN, whole.id)]
    later = await bench_of(conn, [QWEN])
    assert [(line.model, line.run.id) for line in await bench.newest(conn)] == [(QWEN, later.id), (MIMO, cut.id)]


async def test_a_bench_that_runs_and_a_model_without_a_run_in_a_bench_are_left_out(conn):
    running = await bench.create(conn, a_set(), [QWEN], 1)
    await bench.add_result(conn, running.id, QWEN, "c0", 1, "pass", [], ran(), 1000)
    assert await bench.newest(conn) == []
    made = await bench.create(conn, a_set(), [QWEN, MIMO], 1)              # cancelled before the second model was asked
    await bench.add_result(conn, made.id, QWEN, "c0", 1, "error", ["no answer"])
    await bench.finish(conn, made.id, "cancelled")
    assert lines_of(await bench.newest(conn)) == [(QWEN, made.id, [("c0", "error")])]


async def test_a_model_with_another_set_or_asked_without_thinking_is_a_line_of_its_own(conn):
    other_set = cases.parse('name = "Other"\n[[case]]\nid = "c0"\ngroup = "honesty"\nprompt = "q"\n[case.expect]\nanswer_any = ["x"]\n', "own:other")
    plain = await bench_of(conn, [QWEN])
    quiet = await bench_of(conn, [QWEN], thinking_off=True)
    other = await bench_of(conn, [QWEN], set_=other_set)
    elsewhere = await bench_of(conn, [ModelRef(provider="colibri", model="qwen:9b")])      # the same name at another provider is another model
    assert [line.run.id for line in await bench.newest(conn)] == [elsewhere.id, other.id, quiet.id, plain.id]
    again = await bench_of(conn, [QWEN], thinking_off=True)
    assert [line.run.id for line in await bench.newest(conn)] == [again.id, elsewhere.id, other.id, plain.id]
