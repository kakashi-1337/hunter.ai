import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

from hunterai import mcp_manager
from hunterai.mcp_manager import Manager, ServerConfig
from hunterai.providers.base import ToolCall
from hunterai.repo import chats
from hunterai.tools import files, registry, runner
from tests.toolkit import answer_next, run_call, seeded_run

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


@pytest.fixture
async def chunker():
    """The fake server standing in for the file chunker, as the app's MCP manager."""
    m = Manager({"chunker": ServerConfig(name="chunker", command=sys.executable, args=[FAKE],
                                         risk="read", expose=False)})
    await m.start()
    mcp_manager.manager = m

    async def calls(tool):
        return json.loads((await m.call_tool("chunker", "last_calls", {"tool": tool})).text)
    m.calls = calls
    yield m
    mcp_manager.manager = None
    await m.stop()


@pytest.fixture
def files_registered():
    for t in files.tools():
        registry.register(t)


async def one(conn, chat, run_id, tool, **args):
    out, _, steps = await run_call(conn, chat, run_id, ToolCall("c", tool, args))
    return out.block, steps


async def test_symlinks_and_dotdot_are_checked_and_shown_resolved(conn, files_registered, chunker, tmp_path, monkeypatch):   # Review Focus 4
    env = tmp_path / "real.env"; env.write_text("ABLIT_API_KEY=ak_secret_value_123\n"); monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    chat, run_id = await seeded_run(conn, mode="coding")
    work = chats.resolve_workdir(chat); work.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"; outside.mkdir()
    (work / "notes").symlink_to(env); (work / "out").symlink_to(outside)
    for path in ("notes", str(tmp_path / "x" / ".." / "real.env")):
        out, _, steps = await run_call(conn, chat, run_id, ToolCall("c", "read", {"path": path}))
        assert out.block.is_error and "permission policy (protected)" in out.block.content and "ak_secret" not in out.block.content
        assert all(s.kind != "tool_call" for s in steps)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c3", "write", {"path": "out/x.txt", "content": "hi"})))
    req = await answer_next(run_id, "deny")            # coding allows writes inside the work folder; this link leaves it
    assert req.paths == [{"path": str(outside / "x.txt"), "mode": "write"}] and "create" in req.preview
    await task
    assert not (outside / "x.txt").exists()
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c4", "write", {"path": "sub/in.txt", "content": "hi"}))
    assert not out.block.is_error and (work / "sub" / "in.txt").read_text() == "hi"
    decoy = tmp_path / "real.env "; decoy.write_text("decoy")                  # the chunker would strip the space and open real.env
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c5", "read", {"path": str(decoy)}))
    assert out.block.is_error and "whitespace" in out.block.content and "ak_secret" not in out.block.content
    assert await chunker.calls("file_info") == []                               # none of these reads reached the chunker

async def test_line_range_over_the_cap_is_shortened_not_truncated(conn, files_registered, chunker, tmp_path):
    lines = [f"{i:04d}" + "y" * 996 for i in range(1, 301)]                     # 300 lines of 1,000 characters
    wide = tmp_path / "wide.log"; wide.write_text("\n".join(lines) + "\n")
    chat, run_id = await seeded_run(conn)
    seen, start, n = [], 1, 0
    while start:
        n += 1
        out, _, steps = await run_call(conn, chat, run_id, ToolCall(f"c{n}", "read", {"path": str(wide), "start_line": start, "end_line": 300}))
        text = out.block.content
        assert not out.block.is_error and "characters omitted" not in text and len(text) <= files.READ_MAX_CHARS
        assert steps[-1].detail["truncated"] is False
        seen += [l.split("\t", 1)[1] for l in text.splitlines() if "\t" in l]
        nxt = re.search(r'=== next: read\(path="[^"]+", start_line=(\d+)\) ===', text); start = int(nxt.group(1)) if nxt else 0
    assert seen == lines and n > 10 and "=== end of file ===" in text         # every line once, in order
    huge = tmp_path / "one.json"; huge.write_text("{" + "x" * 30_000 + "}\nsecond\n")
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("h1", "read", {"path": str(huge), "start_line": 1}))
    assert out.block.is_error and "line 1 of" in out.block.content and "chunk=1" in out.block.content and "start_line=2" in out.block.content

async def test_large_file_is_read_in_full_chunk_by_chunk(conn, files_registered, chunker, tmp_path):
    lines = [f"line {i} " + "x" * 60 for i in range(1, 2001)]
    big = tmp_path / "big.txt"; big.write_text("\n".join(lines) + "\n")
    chat, run_id = await seeded_run(conn)
    seen, n = [], 1
    while True:
        out, _, _ = await run_call(conn, chat, run_id, ToolCall(f"c{n}", "read", {"path": str(big), "chunk": n}))
        assert not out.block.is_error and "characters omitted" not in out.block.content
        seen += [l.split("\t", 1)[1] for l in out.block.content.splitlines() if "\t" in l]
        if "=== end of big.txt ===" in out.block.content: break
        assert f'=== next: read(path="{big}", chunk={n + 1}) ===' in out.block.content
        n += 1
    assert seen == lines and n > 5
    assert all(c["mode"] == "chars" and c["chunk_size"] == 12_000 and c["path"] == str(big) for c in await chunker.calls("read_chunk"))

async def test_read_without_chunk_starts_with_file_info(conn, files_registered, chunker, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("one\ntwo\n")
    chat, run_id = await seeded_run(conn)
    block, _ = await one(conn, chat, run_id, "read", path=str(f))
    lines = block.content.splitlines()
    assert lines[0] == f"path: {f}" and lines[1].startswith("size:") and any(l.startswith("lines:") for l in lines)
    assert "1\tone" in block.content and "=== end of a.txt ===" in block.content and not block.is_error
    assert len(await chunker.calls("file_info")) == 1 and len(await chunker.calls("read_chunk")) == 1
    block, _ = await one(conn, chat, run_id, "read", path=str(f), chunk=1)
    assert block.content.startswith("=== a.txt | chunk 1/1")          # no header when a chunk is named


async def test_numbered_chunk_over_the_cap_is_refetched_unnumbered(conn, files_registered, chunker, tmp_path):
    f = tmp_path / "thin.txt"
    f.write_text("x\n" * 12_000)
    chat, run_id = await seeded_run(conn)
    block, steps = await one(conn, chat, run_id, "read", path=str(f), chunk=1)
    got = await chunker.calls("read_chunk")
    assert [c["number_lines"] for c in got] == [True, False]
    assert len(block.content) <= runner.RESULT_CAP and steps[-1].detail["truncated"] is False
    assert f'=== next: read(path="{f}", chunk=2) ===' in block.content


async def test_line_range_and_search_modes(conn, files_registered, chunker, tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("\n".join(f"row {i}" if i != 7 else "the needle is here" for i in range(1, 21)) + "\n")
    chat, run_id = await seeded_run(conn)
    block, _ = await one(conn, chat, run_id, "read", path=str(f), start_line=10, end_line=12)
    assert [l.split("\t")[1] for l in block.content.splitlines() if "\t" in l] == ["row 10", "row 11", "row 12"]
    assert (await chunker.calls("read_lines"))[0] == {"path": str(f), "start_line": 10, "end_line": 12, "number_lines": True}
    assert f'=== next: read(path="{f}", start_line=13) ===' in block.content
    block, _ = await one(conn, chat, run_id, "read", path=str(f), search="needle")
    call = (await chunker.calls("search_file"))[0]
    assert (call["pattern"], call["regex"], call["max_hits"]) == ("needle", False, 50)
    assert "L7: the needle is here" in block.content and "(chunk " not in block.content
    assert "chunk numbers use" not in block.content
    assert block.content.rstrip().endswith(f'read a hit with read(path="{f}", start_line=<line>)')
    await one(conn, chat, run_id, "read", path=str(f), search="needle", context=20)
    assert (await chunker.calls("search_file"))[1]["max_hits"] == 1
    block, _ = await one(conn, chat, run_id, "read", path=str(f), search="needle", chunk=2)
    assert block.is_error and "search" in block.content


async def test_binary_file_uses_byte_chunks(conn, files_registered, tmp_path, monkeypatch):
    seen = []

    class Stub:
        def available(self, name):
            return True

        async def call_tool(self, server, tool, arguments, timeout=None):
            seen.append((tool, arguments, timeout))
            from hunterai.tools.base import ToolResult
            if tool == "file_info":
                return ToolResult(f"path: {arguments['path']}\nsize: 300 bytes\ntype: binary (NUL bytes found)")
            return ToolResult("=== b.dat | chunk 1/1 | bytes 0x0-0x12c | 300 bytes ===\n00 01\n=== end of b.dat ===")
    monkeypatch.setattr(mcp_manager, "manager", Stub())
    f = tmp_path / "b.dat"
    f.write_bytes(b"\x00\x01\x02" * 100)
    chat, run_id = await seeded_run(conn)
    block, _ = await one(conn, chat, run_id, "read", path=str(f))
    tool, args, timeout = seen[1]
    assert (tool, args["mode"], args["chunk_size"], timeout) == ("read_chunk", "bytes", 4096, files.CHUNKER_TIMEOUT)
    assert "type: binary" in block.content and not block.is_error


async def test_chunker_error_for_the_file_is_an_error_result(conn, files_registered, chunker, tmp_path):
    f = tmp_path / "s.txt"
    f.write_text("a\n")
    chat, run_id = await seeded_run(conn)
    block, _ = await one(conn, chat, run_id, "read", path=str(f), search="(", regex=True)
    assert block.is_error and "bad regex" in block.content and "chunker unavailable" not in block.content


async def test_chunker_down_gives_a_bounded_plain_read(conn, files_registered, tmp_path):      # no chunker fixture: there is no manager
    lines = [f"l{i}" for i in range(1, 451)]; lines[9] = "z" * 5_000
    f = tmp_path / "plain.txt"; f.write_text("\n".join(lines) + "\n")
    chat, run_id = await seeded_run(conn)
    read = lambda cid, path=f, **a: run_call(conn, chat, run_id, ToolCall(cid, "read", {"path": str(path), **a}))
    out, _, _ = await read("c1")
    text = out.block.content
    assert text.startswith("[chunker unavailable (") and "plain read" in text.splitlines()[0] and not out.block.is_error
    assert "\n200\tl200\n" in text and "201\tl201" not in text and f'=== next: read(path="{f}", start_line=201) ===' in text
    assert "\n10\t" + "z" * 2_000 + " [line cut at 2,000 of 5,000 characters]\n" in text
    out, _, _ = await read("c2", chunk=3)
    assert "\n401\tl401\n" in out.block.content and "\n450\tl450\n" in out.block.content and "=== end of file ===" in out.block.content
    out, _, _ = await read("c3", start_line=199, end_line=202)
    assert [l.split("\t")[0] for l in out.block.content.splitlines() if "\t" in l] == ["199", "200", "201", "202"]
    out, _, _ = await read("c4", search="l1")
    assert out.block.is_error and "repo_search" in out.block.content
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02" * 100)
    out, _, _ = await read("c5", path=tmp_path / "bin.dat")
    assert out.block.is_error and "binary file" in out.block.content
    body = [f"{i:04d}" + "y" * 996 for i in range(1, 301)]                     # 300,000 characters: far over one result
    wide = tmp_path / "wide.log"; wide.write_text("\n".join(body) + "\n")
    seen, start, n = [], 1, 0
    while start:
        n += 1
        out, _, steps = await read(f"w{n}", path=wide, start_line=start)
        text = out.block.content
        assert "characters omitted" not in text and len(text) <= files.READ_MAX_CHARS and steps[-1].detail["truncated"] is False
        seen += [l.split("\t", 1)[1] for l in text.splitlines() if "\t" in l]
        nxt = re.search(r"start_line=(\d+)\) ===", text); start = int(nxt.group(1)) if nxt else 0
    assert seen == body and n > 10
async def test_missing_file_and_directory_are_errors(conn, files_registered, chunker, tmp_path):
    chat, run_id = await seeded_run(conn)
    for path in (tmp_path / "nope.txt", tmp_path):
        block, steps = await one(conn, chat, run_id, "read", path=str(path))
        assert block.is_error and str(path) in block.content
    assert await chunker.calls("file_info") == []


async def test_write_creates_parents_overwrites_atomically_and_keeps_mode(conn, files_registered, tmp_path):
    chat, run_id = await seeded_run(conn, permissions={"risk": {"write": "allow"}})
    f = tmp_path / "deep" / "er" / "a.txt"
    block, _ = await one(conn, chat, run_id, "write", path=str(f), content="hello")
    assert block.content == f"wrote 5 bytes to {f}" and f.read_text() == "hello"
    assert f.stat().st_mode & 0o777 == 0o644
    os.chmod(f, 0o600)
    block, _ = await one(conn, chat, run_id, "write", path=str(f), content="bye!")
    assert f.read_text() == "bye!" and f.stat().st_mode & 0o777 == 0o600 and not block.is_error
    assert [x.name for x in f.parent.iterdir()] == ["a.txt"]            # no temp file left behind


async def test_write_too_large_and_directory_target(conn, files_registered, tmp_path):
    chat, run_id = await seeded_run(conn, permissions={"risk": {"write": "allow"}})
    block, _ = await one(conn, chat, run_id, "write", path=str(tmp_path / "big"),
                         content="x" * (files.MAX_WRITE_BYTES + 1))
    assert block.is_error and "too large" in block.content and not (tmp_path / "big").exists()
    block, _ = await one(conn, chat, run_id, "write", path=str(tmp_path), content="x")
    assert block.is_error and "directory" in block.content


async def test_edit_requires_one_match_unless_replace_all(conn, files_registered, tmp_path):
    chat, run_id = await seeded_run(conn, permissions={"risk": {"write": "allow"}})
    f = tmp_path / "e.txt"
    f.write_text("a = 1\nb = 1\n")
    edit = lambda **a: one(conn, chat, run_id, "edit", path=str(f), **a)   # noqa: E731
    block, _ = await edit(old_string="zzz", new_string="y")
    assert block.is_error and f"old_string not found in {f}" in block.content
    block, _ = await edit(old_string="= 1", new_string="= 2")
    assert block.is_error and "matches 2 times" in block.content and f.read_text() == "a = 1\nb = 1\n"
    block, _ = await edit(old_string="a", new_string="a")
    assert block.is_error
    block, _ = await edit(old_string="a = 1", new_string="a = 5")
    assert block.content == f"replaced 1 occurrence(s) in {f}" and f.read_text() == "a = 5\nb = 1\n"
    block, _ = await edit(old_string="= ", new_string="=", replace_all=True)
    assert block.content == f"replaced 2 occurrence(s) in {f}" and f.read_text() == "a =5\nb =1\n"
    block, _ = await one(conn, chat, run_id, "edit", path=str(tmp_path / "none.txt"),
                         old_string="a", new_string="b")
    assert block.is_error


async def test_edit_preview_is_the_diff_shown_in_the_ask(conn, files_registered, tmp_path):
    chat, run_id = await seeded_run(conn)                              # pentesting: writes ask
    f = tmp_path / "e.txt"
    f.write_text("keep\nold line\nkeep too\n")
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "edit", {
        "path": str(f), "old_string": "old line", "new_string": "new line"})))
    req = await answer_next(run_id, "allow_once")
    out, _, _ = await task
    assert "-old line" in req.preview and "+new line" in req.preview and str(f) in req.preview
    assert req.paths == [{"path": str(f), "mode": "write"}] and req.grant_target == str(f)
    assert f.read_text() == "keep\nnew line\nkeep too\n" and not out.block.is_error


async def test_file_changed_between_ask_and_run_is_not_written(conn, files_registered, tmp_path):
    chat, run_id = await seeded_run(conn)
    f = tmp_path / "e.txt"
    f.write_text("original\n")
    for n, (tool, args) in enumerate((("write", {"content": "mine"}),
                                      ("edit", {"old_string": "original", "new_string": "mine"}))):
        f.write_text("original\n")
        task = asyncio.create_task(run_call(conn, chat, run_id,
                                            ToolCall(f"c{n}", tool, {"path": str(f), **args})))
        while not __import__("hunterai").permissions.pending(run_id):
            await asyncio.sleep(0.01)
        f.write_text("someone else\n")                                 # changed while the ask waits
        await answer_next(run_id, "allow_once")
        out, _, _ = await task
        assert out.block.is_error and "changed since it was shown" in out.block.content
        assert f.read_text() == "someone else\n"
