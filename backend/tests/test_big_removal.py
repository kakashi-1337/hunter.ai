"""A change that takes away a large part of a file asks the owner first, whatever the permission settings say:
30 lines or more, or half or more of a file of 10 lines or more. The answer covers that one call."""
import asyncio

from hunterai.providers.base import ToolCall
from hunterai.repo import grants, settings
from hunterai.tools import files, native
from tests import toolkit
from tests.toolkit import answer_next, seeded_run



async def go(conn, chat, run_id, tc, offered=None):
    """One call through the runner. With a limit: a question nobody answers would wait for ever."""
    return await asyncio.wait_for(toolkit.run_call(conn, chat, run_id, tc, offered=offered), 20)


def lines(first: int, last: int) -> str:
    return "".join(f"line {n}\n" for n in range(first, last + 1))


async def writing_allowed(conn, tmp_path, mode="coding", policy="allow", size=100):
    """A chat whose work folder holds big.py (100 lines), with writing allowed without a question."""
    native.register_all()
    if policy:
        await settings.put(conn, "permissions", {"risk": {"write": policy}})
    chat, run_id = await seeded_run(conn, mode=mode, workdir=str(tmp_path))
    (tmp_path / "big.py").write_text(lines(1, size))
    return chat, run_id


def cut(first: int, last: int, into: str = "") -> dict:
    """An edit that takes lines first..last out of big.py."""
    return {"path": "big.py", "old_string": lines(first, last), "new_string": into}


async def test_a_small_change_goes_through_without_a_question(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path)
    out, events, _ = await go(conn, chat, run_id, ToolCall("c1", "edit", cut(10, 38, "one line instead\n")))     # 29 lines go
    assert not out.block.is_error and all(e["type"] != "permission_request" for e in events)
    assert (tmp_path / "big.py").read_text() == lines(1, 9) + "one line instead\n" + lines(39, 100)
    assert "always asks first" not in str([s.detail for s in _ if s.kind == "permission"])


async def test_an_edit_that_removes_thirty_lines_asks_first_and_the_answer_covers_this_call_only(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "edit", cut(10, 39))))
    req = await answer_next(run_id, "allow_chat")
    out, _, steps = await task
    assert req.warnings == ["this change removes 30 of the 100 lines of big.py"] and req.can_allow_chat is False
    assert req.reasons[-1] == "a change that takes away this much of a file always asks first, whatever the settings say"
    assert "-line 10\n" in req.preview and not out.block.is_error and await grants.list(conn, chat.id) == []
    assert (tmp_path / "big.py").read_text() == lines(1, 9) + lines(40, 100)
    assert [s.detail["warnings"] for s in steps if s.kind == "permission"] == [["this change removes 30 of the 100 lines of big.py"]]


async def test_a_denied_removal_leaves_the_file_as_it_was(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "edit", cut(1, 60))))
    await answer_next(run_id, "deny", note="keep those")
    out, _, _ = await task
    assert out.block.content == "The owner denied this call. Owner's note: keep those" and (tmp_path / "big.py").read_text() == lines(1, 100)


async def test_a_file_written_over_with_half_of_it_gone_asks_first(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path, size=20)
    out, events, _ = await go(conn, chat, run_id, ToolCall("c1", "write", {"path": "big.py", "content": lines(1, 11)}))
    assert not out.block.is_error and all(e["type"] != "permission_request" for e in events)         # 9 of 20 go: under half
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c2", "write", {"path": "big.py", "content": lines(1, 5) + "new\n"})))
    req = await answer_next(run_id, "allow_once")
    await task
    assert req.warnings == ["this change removes 6 of the 11 lines of big.py"] and (tmp_path / "big.py").read_text() == lines(1, 5) + "new\n"


async def test_a_short_file_can_be_rewritten_without_a_question(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path, size=9)
    out, events, _ = await go(conn, chat, run_id, ToolCall("c1", "write", {"path": "big.py", "content": "all new\n"}))
    assert not out.block.is_error and all(e["type"] != "permission_request" for e in events)


async def test_what_only_adds_never_asks(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path)
    for n, call in enumerate([ToolCall("c1", "write", {"path": "new.py", "content": lines(1, 500)}),
                              ToolCall("c2", "write", {"path": "big.py", "content": lines(1, 100) + lines(101, 400)}),
                              ToolCall("c3", "edit", {"path": "big.py", "old_string": "line 5\n", "new_string": "line 5\n" + lines(1000, 1100)})]):
        out, events, _ = await go(conn, chat, run_id, call)
        assert not out.block.is_error and all(e["type"] != "permission_request" for e in events), n


async def test_coding_mode_writes_in_its_work_folder_without_a_question_but_not_a_large_removal(conn, tmp_path):
    """Nothing is set: coding mode lets the model change files in the work folder by itself."""
    chat, run_id = await writing_allowed(conn, tmp_path, policy=None)
    out, events, _ = await go(conn, chat, run_id, ToolCall("c1", "edit", cut(1, 1, "LINE 1\n")))
    assert not out.block.is_error and all(e["type"] != "permission_request" for e in events)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c2", "edit", cut(2, 61))))
    req = await answer_next(run_id, "deny")
    await task
    assert req.warnings == ["this change removes 60 of the 100 lines of big.py"]


async def test_a_grant_for_the_file_does_not_cover_a_large_removal_from_it(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path, mode="pentesting", policy="ask")
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "edit", cut(1, 1, "LINE 1\n"))))
    req = await answer_next(run_id, "allow_chat")
    await task
    assert req.warnings == [] and req.can_allow_chat and len(await grants.list(conn, chat.id)) == 1
    out, events, _ = await go(conn, chat, run_id, ToolCall("c2", "edit", cut(2, 2, "LINE 2\n")))       # granted: no question
    assert not out.block.is_error and all(e["type"] != "permission_request" for e in events)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c3", "edit", cut(3, 60))))
    req = await answer_next(run_id, "deny")
    await task
    assert req.warnings == ["this change removes 58 of the 100 lines of big.py"] and not req.can_allow_chat
    assert (tmp_path / "big.py").read_text() == "LINE 1\nLINE 2\n" + lines(3, 100)


async def test_a_change_the_policy_denies_stays_denied_without_a_question(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path, policy="deny")
    out, events, _ = await go(conn, chat, run_id, ToolCall("c1", "edit", cut(1, 60)), offered={"edit"})     # as when the policy changes mid-run
    assert "permission policy" in out.block.content and all(e["type"] != "permission_request" for e in events)


async def test_the_rule_holds_outside_coding_mode_too(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path, mode="pentesting")
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "edit", cut(1, 60))))
    req = await answer_next(run_id, "deny")
    await task
    assert req.warnings == ["this change removes 60 of the 100 lines of big.py"]


async def test_a_file_that_is_not_text_is_not_written_over_without_a_question(conn, tmp_path):
    chat, run_id = await writing_allowed(conn, tmp_path)
    (tmp_path / "data.bin").write_bytes(b"\xff\xfe" + bytes(range(256)) * 10)
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "write", {"path": "data.bin", "content": "text now\n"})))
    req = await answer_next(run_id, "deny")
    await task
    assert req.warnings == ["this replaces the whole of data.bin, which is not text (2,562 bytes)"] and not req.can_allow_chat


async def test_removed_lines_are_counted_whatever_they_begin_with(conn, tmp_path):
    """A line of SQL comment begins with two dashes, like the head of the comparison that is shown."""
    chat, run_id = await writing_allowed(conn, tmp_path)
    (tmp_path / "big.py").write_text("".join(f"-- note {n}\n" for n in range(40)))
    task = asyncio.create_task(go(conn, chat, run_id, ToolCall("c1", "write", {"path": "big.py", "content": "-- note 0\n"})))
    req = await answer_next(run_id, "deny")
    await task
    assert req.warnings == ["this change removes 39 of the 40 lines of big.py"]


def test_what_counts_as_a_large_removal():
    big = files.large_removal
    assert big({"removes": 30, "of": 1000, "name": "a.py"}) == "this change removes 30 of the 1,000 lines of a.py"
    assert big({"removes": 29, "of": 1000, "name": "a.py"}) is None
    assert big({"removes": 5, "of": 10, "name": "a.py"}) == "this change removes 5 of the 10 lines of a.py"
    assert big({"removes": 4, "of": 9, "name": "a.py"}) is None and big({"removes": 9, "of": 9, "name": "a.py"}) is None
    assert big({"removes": 0, "of": 0, "name": "a.py"}) is None and big({}) is None and big({"sha256": "x", "tool": "write"}) is None
