"""The check of a work folder: a command the owner set for it (the project's tests). In coding mode HunterAI runs
it before a run's first change and after the run, and the answer shows both outcomes beside the list of what
the run changed. Real commands, real folders."""
import asyncio

import pytest

from hunterai import checks, savepoint
from hunterai.providers.base import ProviderError, TextDelta, ToolCall
from hunterai.repo import chats, messages, runs, settings
from hunterai.tools import native
from hunterai.types import TextBlock
from tests.test_api import client  # noqa: F401 (a fixture)
from tests.test_engine import go, no_bytes, use
from tests.test_savepoint import block, coding_chat, work, write  # noqa: F401 (work is a fixture)
from tests.toolkit import Turns

RETURNS_2 = 'grep -q "return 2" app.py'            # fails on the folder as the fixture makes it, passes once app.py returns 2
RETURNS_1 = 'grep -q "return 1" app.py'            # the other way round
FIX = ToolCall("c1", "edit", {"path": "app.py", "old_string": "return 1", "new_string": "return 2"})


async def checked(conn, work, command, **more):                         # noqa: F811
    chat, user = await coding_chat(conn, work, **more)
    await checks.set_command(conn, work, command)
    return chat, user


def check_steps(run) -> list:
    return [s for s in run.steps if s.kind == "check"]


# ---- the setting

async def test_the_check_is_kept_for_the_folder_and_every_chat_in_it_has_it(client, conn, work, tmp_path):      # noqa: F811
    one, _ = await coding_chat(conn, work)
    two, _ = await coding_chat(conn, work)
    other, _ = await coding_chat(conn, tmp_path / "elsewhere")
    assert (await client.get(f"/api/chats/{one.id}/check")).json() == {"folder": str(work), "command": ""}
    r = await client.put(f"/api/chats/{one.id}/check", json={"command": "  pytest -q  "})
    assert r.status_code == 200 and r.json() == {"folder": str(work), "command": "pytest -q"}
    assert (await client.get(f"/api/chats/{two.id}/check")).json()["command"] == "pytest -q"
    assert (await client.get(f"/api/chats/{other.id}/check")).json()["command"] == ""
    assert (await client.put(f"/api/chats/{two.id}/check", json={"command": ""})).json()["command"] == ""
    assert (await client.get(f"/api/chats/{one.id}/check")).json()["command"] == ""
    assert (await client.get("/api/chats/nope/check")).status_code == 404


@pytest.mark.parametrize("command, says", [
    ("x" * 2001, "too long"),
    ("pytest -q\nrm -rf /", "one line"),
    ("curl http://127.0.0.1:8790/api/settings", "HunterAI's own API"),
])
async def test_a_command_that_cannot_be_the_check_is_refused_with_the_reason(client, conn, work, command, says):     # noqa: F811
    chat, _ = await coding_chat(conn, work)
    r = await client.put(f"/api/chats/{chat.id}/check", json={"command": command})
    assert r.status_code == 400 and says in r.json()["detail"]
    assert (await client.get(f"/api/chats/{chat.id}/check")).json()["command"] == ""


async def test_a_command_with_a_key_hunterai_keeps_is_refused(client, conn, work, monkeypatch):      # noqa: F811
    monkeypatch.setenv("ABLIT_API_KEY", "ak_secret_value_123")
    chat, _ = await coding_chat(conn, work)
    r = await client.put(f"/api/chats/{chat.id}/check", json={"command": "curl -H 'x: ak_secret_value_123' https://example.com"})
    assert r.status_code == 400 and "key" in r.json()["detail"] and "ak_secret_value_123" not in r.text


# ---- before and after a run

async def test_a_run_that_makes_the_check_pass_says_so_under_its_answer(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await checked(conn, work, RETURNS_2)
    run, msg = await go(conn, chat, user)
    found = block(msg).check
    assert found["command"] == RETURNS_2 and found["note"] == ""
    assert (found["before"]["passed"], found["before"]["exit_code"]) == (False, 1)
    assert (found["after"]["passed"], found["after"]["exit_code"]) == (True, 0)
    assert [(s.name, s.status, s.detail["when"], s.detail["passed"]) for s in check_steps(run)] == [
        (RETURNS_2, "done", "before", False), (RETURNS_2, "done", "after", True)]
    assert run.status == "done" and block(msg).files == [{"path": "app.py", "change": "changed"}]


async def test_a_run_that_breaks_the_check_says_so_and_shows_what_the_check_wrote(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Changed.")]))
    chat, user = await checked(conn, work, 'grep -q "return 1" app.py || { echo "FAILED: main() no longer returns 1"; exit 3; }')
    run, msg = await go(conn, chat, user)
    found = block(msg).check
    assert found["before"]["passed"] is True and found["before"]["output"] == ""
    assert (found["after"]["passed"], found["after"]["exit_code"], found["after"]["timed_out"]) == (False, 3, False)
    assert found["after"]["output"] == "FAILED: main() no longer returns 1\n" and found["after"]["seconds"] >= 0


async def test_the_first_change_waits_for_the_check_of_the_folder_as_it_was(conn, work):      # noqa: F811
    """The check before the run sees the folder before anything was changed, not half way through."""
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await checked(conn, work, "cat app.py")
    run, msg = await go(conn, chat, user)
    found = block(msg).check
    assert found["before"]["output"] == "def main():\n    return 1\n" and found["after"]["output"] == "def main():\n    return 2\n"


async def test_a_folder_that_is_as_the_last_check_saw_it_is_not_checked_again(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")],
                           [ToolCall("c2", "write", {"path": "more.py", "content": "y = 2\n"})], [TextDelta("Added.")]))
    chat, user = await checked(conn, work, RETURNS_2)
    first, msg = await go(conn, chat, user)
    again = await messages.add(conn, chat.id, msg.id, "user", [TextBlock(text="add more.py")])
    second, msg2 = await go(conn, chat, again)
    assert [s.detail["when"] for s in check_steps(first)] == ["before", "after"]
    assert [s.detail["when"] for s in check_steps(second)] == ["after"]           # before: what the check said after the first run
    assert block(msg2).check["before"] == block(msg).check["after"]


async def test_a_folder_that_changed_between_two_runs_is_checked_before_the_second(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")],
                           [ToolCall("c2", "write", {"path": "more.py", "content": "y = 2\n"})], [TextDelta("Added.")]))
    chat, user = await checked(conn, work, RETURNS_2)
    first, msg = await go(conn, chat, user)
    write(work, "app.py", "def main():\n    return 9\n")                # the owner's own change, between the runs
    again = await messages.add(conn, chat.id, msg.id, "user", [TextBlock(text="add more.py")])
    second, msg2 = await go(conn, chat, again)
    assert [s.detail["when"] for s in check_steps(second)] == ["before", "after"]
    assert block(msg2).check["before"]["passed"] is False


async def test_what_the_check_itself_writes_is_not_listed_as_the_runs(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await checked(conn, work, "date >> check.log; true")
    run, msg = await go(conn, chat, user)
    assert block(msg).files == [{"path": "app.py", "change": "changed"}] and (work / "check.log").read_text().count("\n") == 2


async def test_a_run_that_changes_nothing_is_not_followed_by_a_check(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [ToolCall("c1", "execute", {"command": "ls"})], [TextDelta("Listed.")]))
    chat, user = await checked(conn, work, RETURNS_2)
    run, msg = await go(conn, chat, user)
    assert block(msg) is None and [s.detail["when"] for s in check_steps(run)] == ["before"]


async def test_a_run_that_only_reads_runs_no_check(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [ToolCall("c1", "read", {"path": "app.py"})], [TextDelta("It returns 1.")]))
    chat, user = await checked(conn, work, RETURNS_2)
    run, msg = await go(conn, chat, user)
    assert check_steps(run) == [] and block(msg) is None


async def test_a_folder_without_a_check_gets_a_list_without_one(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    assert block(msg).check is None and check_steps(run) == []


async def test_other_modes_run_no_check(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await checked(conn, work, RETURNS_2, mode="pentesting")
    run, msg = await go(conn, chat, user)
    assert check_steps(run) == [] and (work / "app.py").read_text() == "def main():\n    return 2\n"


async def test_a_folder_without_a_save_point_runs_no_check(conn, work, monkeypatch):      # noqa: F811
    monkeypatch.setattr(savepoint, "MAX_FILES", 2)
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await checked(conn, work, RETURNS_2)
    run, msg = await go(conn, chat, user)
    assert check_steps(run) == [] and block(msg).check is None and block(msg).reason.startswith("No save point")


async def test_a_run_whose_changes_cannot_be_listed_is_not_followed_by_a_check(conn, work, monkeypatch):      # noqa: F811
    """The folder cannot be read at the end of the run (the run filled it past the limit): there is no list to hold a check against."""
    monkeypatch.setattr(savepoint, "MAX_FILES", 10)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "execute", {"command": "for i in $(seq 1 30); do echo x > made_$i.txt; done"})],
                           [TextDelta("Made them.")]))
    chat, user = await checked(conn, work, RETURNS_2)
    run, msg = await go(conn, chat, user)
    assert [s.detail["when"] for s in check_steps(run)] == ["before"]
    assert block(msg).check is None and block(msg).reason.startswith("What this run changed could not be listed at its end")


async def test_a_run_that_failed_is_not_followed_by_a_check_and_the_list_says_so(conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], ProviderError("unavailable", "the provider went away", 503),
                           ProviderError("unavailable", "still away", 503)))
    chat, user = await checked(conn, work, RETURNS_2)
    run, msg = await go(conn, chat, user)
    found = block(msg).check
    assert run.status == "error" and [s.detail["when"] for s in check_steps(run)] == ["before"]
    assert found["before"]["passed"] is False and found["after"] is None
    assert found["note"] == "No check followed this run: it was stopped, or it failed."


async def test_a_check_that_does_not_end_is_stopped_and_counts_as_failed(conn, work, monkeypatch):      # noqa: F811
    monkeypatch.setattr(checks, "SECONDS", 0.5)
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await checked(conn, work, 'grep -q "return 1" app.py || sleep 30')
    run, msg = await go(conn, chat, user)
    after = block(msg).check["after"]
    assert (after["passed"], after["timed_out"], after["exit_code"]) == (False, True, None) and run.status == "done"
    assert check_steps(run)[-1].detail["timed_out"] is True


async def test_stopping_the_run_during_the_check_ends_the_check_and_keeps_the_answer(conn, work):      # noqa: F811
    from hunterai import db, engine, procs
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    chat, user = await checked(conn, work, 'grep -q "return 1" app.py || sleep 30')
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    for _ in range(500):                                                # until the check after the run is going
        going = [s for s in (await runs.get(conn, run_id)).steps if s.kind == "check" and s.detail.get("when") == "after"]
        if going and procs.running(chat.id):
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the check after the run never started")
    await engine.cancel(run_id)
    await asyncio.wait_for(engine.wait(run_id), 20)
    for _ in range(250):                                                # the process is ended at once; that it is gone is noticed a moment later
        if not procs.running(chat.id):
            break
        await asyncio.sleep(0.02)
    run, msg = await runs.get(conn, run_id), await messages.get(conn, mid)
    assert run.status == "done" and procs.running(chat.id) == []
    assert [b.text for b in msg.blocks if b.type == "text"] == ["Fixed."]
    found = block(msg).check
    assert found["after"] is None and found["note"] == "The check after this run was stopped before it ended."
    assert check_steps(run)[-1].status == "cancelled"


async def test_the_output_of_the_check_is_kept_by_its_end_and_without_keys(conn, work, monkeypatch):      # noqa: F811
    monkeypatch.setenv("ABLIT_API_KEY", "ak_secret_value_123")
    monkeypatch.setattr(checks, "OUTPUT", 200)
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Fixed.")]))
    command = 'for i in $(seq 1 200); do echo "line $i"; done; printf "key ak_secret_%s (%s).\\n" value_123 "$ABLIT_API_KEY"; echo THE END'
    chat, user = await checked(conn, work, command)
    run, msg = await go(conn, chat, user)
    out = block(msg).check["after"]["output"]
    assert out.endswith("key [redacted] ().\nTHE END\n") and out.startswith("[the output before this is left out]\n") and "line 1\n" not in out
    assert "ak_secret_value_123" not in msg.model_dump_json() and len(out) < 300


async def test_the_check_is_the_owners_and_never_goes_to_a_model(conn, work):      # noqa: F811
    p = Turns("abliteration", [FIX], [TextDelta("Fixed.")], [TextDelta("Yes.")])
    use(abliteration=p)
    chat, user = await checked(conn, work, "echo CHECK-OUTPUT-7731")
    run, msg = await go(conn, chat, user)
    again = await messages.add(conn, chat.id, msg.id, "user", [TextBlock(text="sure?")])
    await go(conn, chat, again)
    assert "CHECK-OUTPUT-7731" in msg.model_dump_json()
    assert "CHECK-OUTPUT-7731" not in str([b for m in p.calls[-1]["messages"] for b in m.blocks])


async def test_undo_still_works_on_a_list_with_a_check(client, conn, work):      # noqa: F811
    use(abliteration=Turns("abliteration", [FIX], [TextDelta("Changed.")]))
    chat, user = await checked(conn, work, RETURNS_1)
    run, msg = await go(conn, chat, user)
    got = (await client.post(f"/api/runs/{run.id}/undo")).json()
    assert got["restored"] == ["app.py"] and (work / "app.py").read_text() == "def main():\n    return 1\n"
    assert next(b for b in got["message"]["blocks"] if b["type"] == "changes")["check"]["after"]["passed"] is False


# ---- with the /fix skill

async def test_a_fix_started_with_its_command_runs_with_the_list_and_the_check(conn, work):      # noqa: F811
    """The three parts of the break-fix method together: the skill's instructions reach the model, and what the model
    then did stands under its answer, held against the folder's check."""
    prov = Turns("abliteration", [FIX], [TextDelta("Cause: main() returned 1. Changed app.py. The check passes now.")])
    use(abliteration=prov)
    chat, user = await checked(conn, work, RETURNS_2, text="/fix main() must return 2")
    run, msg = await go(conn, chat, user)
    system = prov.calls[0]["system"]
    assert 'started the skill "fix"' in system and "# Fix it, and prove it" in system and "Take it back out" in system
    found = block(msg)
    assert found.files == [{"path": "app.py", "change": "changed"}]
    assert (found.check["before"]["passed"], found.check["after"]["passed"]) == (False, True)
    assert (await chats.get(conn, chat.id)).title == "main() must return 2"
