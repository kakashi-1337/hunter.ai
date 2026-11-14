import asyncio
import os
import stat

import httpx
import pytest
import respx

from hunterai.providers.base import ToolCall
from hunterai.tools import registry, search
from hunterai.tools.base import ToolError
from hunterai.tools.search import SearchResult
from tests.toolkit import alive, answer_next, eventually, run_call, seeded_run


@pytest.fixture(autouse=True)
def search_registered():
    for t in search.tools():
        registry.register(t)


async def find(conn, root, **args):
    chat, run_id = await seeded_run(conn)
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "repo_search", {"path": str(root), **args}))
    return out.block.content


class FakeSearch:
    name = "fake"
    def __init__(self, results): self.results, self.queries = results, []
    async def search(self, query, *, limit, page=1):
        self.queries.append((query, limit, page)); return self.results[:limit]

async def test_web_search_formats_results_and_grant_covers_all_searches(conn, search_registered):
    fake = FakeSearch([SearchResult(title="Title", url="https://x.test/a", snippet="s" * 400, engine="e"), SearchResult(title="Two", url="https://x.test/b")])
    search.set_provider(fake)
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "web_search", {"query": "sqli cheat sheet"})))
    req = await answer_next(run_id, "allow_chat")                        # pentesting: network asks
    out, _, _ = await task
    assert (req.grant_target, req.hosts, req.can_allow_chat) == ("search", [], True)
    assert out.block.content == "1. Title\n   https://x.test/a\n   " + "s" * 300 + "\n2. Two\n   https://x.test/b\n   "
    out, events, _ = await run_call(conn, chat, run_id, ToolCall("c2", "web_search", {"query": "another", "limit": 1, "page": 2}))
    assert all(e["type"] != "permission_request" for e in events) and out.block.content.count("https://") == 1
    assert fake.queries == [("sqli cheat sheet", 8, 1), ("another", 1, 2)]
    search.set_provider(FakeSearch([]))
    assert (await run_call(conn, chat, run_id, ToolCall("c3", "web_search", {"query": "x"})))[0].block.content == "no results"

async def test_pattern_and_glob_cannot_become_flags(conn, search_registered, tmp_path):
    (tmp_path / "a.txt").write_text("plain --hidden text\n"); (tmp_path / ".dot.txt").write_text("in a dotfile --hidden\n")
    (tmp_path / ".ignore").write_text("skipped.txt\n"); (tmp_path / "skipped.txt").write_text("ignored --hidden\n")
    text = await find(conn, tmp_path, pattern="--hidden")                # searched as text, not taken as a flag
    assert "a.txt:1:plain --hidden text" in text and ".dot.txt" not in text and "skipped.txt" not in text
    text = await find(conn, tmp_path, pattern="--hidden", glob="--no-ignore")     # used as a glob that matches no file
    assert text.splitlines()[-1] == "no matches"

async def test_sensitive_files_under_the_root_are_never_searched(conn, search_registered, tmp_path, monkeypatch):   # Ruling 28
    for name in (".env", ".env.local", "server.pem", "sub/app.key", "sub/id_rsa", ".netrc", "notes.txt"):
        f = tmp_path / name; f.parent.mkdir(exist_ok=True); f.write_text(f"needle in {name}\n")
    conf = tmp_path / "rgconf"; conf.write_text("--hidden\n--no-ignore\n"); monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(conf))
    for glob in (None, ".env*", "*.pem", "**/*", "sub/**"):
        text = await find(conn, tmp_path, pattern="needle", **({"glob": glob} if glob else {}))
        assert "needle in" not in text.replace("needle in notes.txt", "")          # nothing but notes.txt is ever shown
    assert "notes.txt:1:needle in notes.txt" in await find(conn, tmp_path, pattern="needle")
@respx.mock
async def test_searxng_request_and_mapping():
    route = respx.get("http://127.0.0.1:8888/search").mock(return_value=httpx.Response(200, json={"results": [
        {"title": "A", "url": "https://a.test", "content": "about a", "engine": "ddg"},
        {"title": "B", "url": "https://b.test"}, {"title": "no url"}]}))
    got = await search.SearxngProvider().search("sqli", limit=5, page=2)
    assert dict(route.calls.last.request.url.params) == {"q": "sqli", "format": "json", "pageno": "2", "safesearch": "0"}
    assert got == [SearchResult(title="A", url="https://a.test", snippet="about a", engine="ddg"),
                   SearchResult(title="B", url="https://b.test")]
    assert len(await search.SearxngProvider().search("sqli", limit=1)) == 1


@respx.mock
async def test_searxng_down_and_json_refused():
    respx.get("http://127.0.0.1:8888/search").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ToolError, match=r"not running at http://127.0.0.1:8888.*scripts/searxng.sh start"):
        await search.SearxngProvider().search("x", limit=3)
    respx.get("http://127.0.0.1:8888/search").mock(return_value=httpx.Response(403))
    with pytest.raises(ToolError, match="refused the JSON format"):
        await search.SearxngProvider().search("x", limit=3)
    respx.get("http://127.0.0.1:8888/search").mock(return_value=httpx.Response(500))
    with pytest.raises(ToolError, match="HTTP 500"):
        await search.SearxngProvider().search("x", limit=3)


async def test_repo_search_finds_lines_with_numbers(conn, tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ntoken = 'abc'\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("# TOKEN here\n")
    text = await find(conn, tmp_path, pattern="token", ignore_case=True)
    assert text.splitlines()[0] == f"root: {tmp_path}"
    assert "a.py:2:token = 'abc'" in text and "sub/b.py:1:# TOKEN here" in text
    assert "sub/b.py" not in await find(conn, tmp_path, pattern="token")
    one = await find(conn, tmp_path / "a.py", pattern="token")
    assert "2:token = 'abc'" in one and one.splitlines()[0] == f"root: {tmp_path / 'a.py'}"


async def test_env_file_inside_the_root_is_excluded(conn, tmp_path, monkeypatch):
    env = tmp_path / "keys.txt"
    env.write_text("needle secret\n")
    (tmp_path / "ok.txt").write_text("needle fine\n")
    (tmp_path / "server.pem").write_text("needle pem\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    text = await find(conn, tmp_path, pattern="needle")
    assert "ok.txt:1:needle fine" in text and "secret" not in text and "pem" not in text.replace("server.pem", "")
    assert "permission policy (protected)" in await find(conn, env, pattern="needle")
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c", "repo_search", {
        "path": str(tmp_path / "server.pem"), "pattern": "needle"})))
    req = await answer_next(run_id, "deny")
    await task
    assert any("usually holds credentials" in w for w in req.warnings)


async def test_max_results_stops_early(conn, tmp_path):
    (tmp_path / "many.txt").write_text("hit\n" * 1000)
    text = await find(conn, tmp_path, pattern="hit", max_results=5)
    assert len([ln for ln in text.splitlines() if ":hit" in ln]) == 5
    assert text.splitlines()[-1].startswith("[stopped at 5 matching lines")


async def test_no_matches_and_bad_regex(conn, tmp_path):
    (tmp_path / "a.txt").write_text("call(x)\n")
    assert (await find(conn, tmp_path, pattern="zzz")).splitlines()[-1] == "no matches"
    assert "ripgrep:" in await find(conn, tmp_path, pattern="(")
    assert "a.txt:1:call(x)" in await find(conn, tmp_path, pattern="(", fixed_strings=True)


async def test_hung_rg_is_killed_by_the_timeout(conn, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "rg"
    fake.write_text(f"#!/bin/sh\necho $$ > {tmp_path}/rg.pid\nexec sleep 300\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(search, "RG_TIMEOUT", 0.3)
    (tmp_path / "data").mkdir()
    assert "timed out" in await find(conn, tmp_path / "data", pattern="x")
    await eventually(lambda: not alive(int((tmp_path / "rg.pid").read_text())))


async def test_missing_rg_is_a_clear_error(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert "ripgrep (rg) is not installed" in await find(conn, tmp_path, pattern="x")
