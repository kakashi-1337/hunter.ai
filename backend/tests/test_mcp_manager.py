import asyncio
import contextlib
import dataclasses
import json
import re
import sys
import time
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client

from hunterai import config, mcp_manager, permissions
from hunterai.mcp_manager import Manager, ServerConfig
from hunterai.permissions import Policy
from hunterai.providers.base import ToolCall
from hunterai.repo import chats
from hunterai.tools import registry
from hunterai.tools.base import HostUse, PathUse, ToolContext, ToolError
from tests.toolkit import alive, answer_next, eventually, fake_tool, run_call, seeded_run

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


@pytest.fixture
async def fake():
    """fake(**cfg): a started Manager with one server named "fake"; stopped on teardown."""
    made: list[Manager] = []

    async def make(args=None, wait=None, **cfg):
        m = Manager({"fake": ServerConfig(name="fake", command=sys.executable,
                                          args=args or [FAKE], **cfg)})
        made.append(m)
        await m.start(wait)
        return m
    yield make
    for m in made:
        await m.stop()


async def test_tools_register_under_the_server_prefix_with_the_config_risk(fake):
    m = await fake()
    names = [t.name for t in registry.all()]
    assert "fake_echo" in names and registry.get("fake_echo").risk == "execute" and registry.get("fake_echo").source == "mcp:fake"
    assert m.status()[0].state == "ready" and "fake_echo" in m.status()[0].tools
    assert (await m.call_tool("fake", "echo", {"text": "hi"})).text == "hi"
    assert (await m.call_tool("fake", "fail", {})).is_error

async def test_dead_server_restarts_once_then_stays_dead(fake):
    m = await fake()
    with pytest.raises(ToolError): await m.call_tool("fake", "die", {})
    await eventually(lambda: m.status()[0].state == "ready")
    assert m.status()[0].restarts == 1 and registry.get("fake_echo") is not None
    with pytest.raises(ToolError): await m.call_tool("fake", "die", {})
    await eventually(lambda: m.status()[0].state == "dead")
    assert registry.get("fake_echo") is None and m.status()[0].error and not m.available("fake")

async def test_stop_ends_the_server_and_its_children(fake):
    m = await fake()
    child = int((await m.call_tool("fake", "spawn_child", {})).text)
    await m.stop()
    await eventually(lambda: not alive(child))

async def test_children_get_no_keys(fake, monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "ak_secret_value_123"); monkeypatch.setenv("OTHERTOOL_LLM", "x")
    names = (await (await fake(env={"FAKE_FLAG": "1"})).call_tool("fake", "env_names", {})).text.splitlines()
    assert "FAKE_FLAG" in names and "PATH" in names and not [n for n in names if n.endswith("_API_KEY") or n.startswith("OTHERTOOL_")]

def test_defaults_and_config_file(isolated_home):
    d = mcp_manager.default_servers()["chunker"]
    assert (d.command, d.args, d.risk, d.expose) == (sys.executable, [str(config.chunker_path())], "read", False)
    assert ServerConfig(name="x", command="y").risk == "execute"
    config.mcp_file().write_text(json.dumps({"servers": {
        "chunker": {"command": "evil", "expose": True, "risk": "execute", "env": {"CHUNKER_MAX_MB": "50"}},
        "Bad Name": {"command": "x"}, "scan": {"command": "scan-mcp", "risk": "network"}}}))
    servers, error = mcp_manager.load_config()
    c = servers["chunker"]
    assert (c.command, c.expose, c.risk, c.env) == (sys.executable, False, "read", {"CHUNKER_MAX_MB": "50"})
    assert servers["scan"].risk == "network" and "Bad Name" not in servers and "Bad Name" in error
    config.mcp_file().write_text("{not json"); servers, error = mcp_manager.load_config()
    assert list(servers) == ["chunker", "rea", "context7"] and error

async def test_generic_prepare_names_paths_and_hosts_without_a_lookup(tmp_path, monkeypatch):   # no dns fixture: a lookup would fail the test
    env = tmp_path / "keys.env"; env.write_text("K=v"); monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    ctx = ToolContext(chat_id="c", run_id="r", step_id="", call_id="k", mode="pentesting", target=None, workdir=tmp_path, cancel=asyncio.Event())
    args = {"path": "a/../b.txt", "url": "https://x.test/p", "n": 3}
    p = await mcp_manager.generic_prepare("execute", ctx, args)
    assert p.paths == (PathUse(str(tmp_path / "b.txt"), "write"),) and p.hosts == (HostUse("x.test", 443, ()),)
    assert p.resolve_hosts and p.url_path == "/p" and re.fullmatch(r"[0-9a-f]{16}", p.grant_target)
    assert (await mcp_manager.generic_prepare("execute", ctx, dict(args))).grant_target == p.grant_target
    assert (await mcp_manager.generic_prepare("read", ctx, args)).paths[0].mode == "read"
    deep = await mcp_manager.generic_prepare("read", ctx, {"files": [str(env) + " "], "opts": {"req": {"url": "http://127.0.0.1:8790/"}, "dir": "sub"}})
    assert PathUse(str(env), "read") in deep.paths and PathUse(str(tmp_path / "sub"), "read") in deep.paths
    assert deep.hosts == (HostUse("127.0.0.1", 8790, ("127.0.0.1",)),)
    tool, _ = fake_tool("x_read", risk="read", source="mcp:x")
    kw = dict(mode="pentesting", tool=tool, target=None, workdir=tmp_path, global_policy=Policy(), chat_policy=Policy(), grants=[])
    assert permissions.decide(prepared=deep, **kw).rule == "protected"                          # nested env file: denied
    assert permissions.decide(prepared=dataclasses.replace(deep, paths=()), **kw).rule == "own-api"   # nested URL to HunterAI's API: denied
    with pytest.raises(ToolError, match="whitespace"): await mcp_manager.generic_prepare("read", ctx, {"path": "notes.txt "})
    assert mcp_manager.resolved_args({"path": "a/../b.txt", "opts": {"dir": "sub"}, "n": 3}, tmp_path) == \
        {"path": str(tmp_path / "b.txt"), "opts": {"dir": str(tmp_path / "sub")}, "n": 3}

async def test_mcp_tool_goes_through_the_runner_and_asks_by_default(conn, fake):                # a third-party server: ask
    await fake()
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "fake_echo", {"text": "hi"})))
    req = await answer_next(run_id, "allow_once")
    out, _, steps = await task
    assert (req.source, req.risk, req.arguments) == ("mcp:fake", "execute", {"text": "hi"}) and out.block.content == "hi"
    assert [(s.kind, s.status) for s in steps] == [("permission", "done"), ("tool_call", "done")]

async def test_mcp_path_arguments_are_sent_resolved(conn, fake):                                # Review Focus 4
    m = await fake(cwd="/")                                                                     # the server's own cwd must not matter
    chat, run_id = await seeded_run(conn)
    work = chats.resolve_workdir(chat)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "fake_echo_path", {"path": "a/../b.txt"})))
    req = await answer_next(run_id, "allow_once")
    out, _, _ = await task
    assert req.paths == [{"path": str(work / "b.txt"), "mode": "write"}] and out.block.content == str(work / "b.txt")
    assert json.loads((await m.call_tool("fake", "last_calls", {"tool": "echo_path"})).text) == [{"path": str(work / "b.txt")}]

async def test_timed_out_call_restarts_the_server(fake):                                        # Ruling 27
    m = await fake()
    child = int((await m.call_tool("fake", "spawn_child", {})).text)
    with pytest.raises(ToolError, match="timed out"): await m.call_tool("fake", "slow", {"seconds": 30}, timeout=0.2)
    await eventually(lambda: not alive(child))                                                  # the busy server and its children are gone
    await eventually(lambda: m.status()[0].state == "ready")
    assert (m.status()[0].restarts, m.status()[0].forced_restarts) == (0, 1)
    assert (await m.call_tool("fake", "echo", {"text": "back"})).text == "back"

async def test_grandchild_dies_when_the_server_dies(fake):
    m = await fake()
    child = int((await m.call_tool("fake", "spawn_child", {})).text)
    with pytest.raises(ToolError): await m.call_tool("fake", "die", {})
    await eventually(lambda: not alive(child))

async def test_http_transport_opens_the_configured_url(monkeypatch):
    seen = []
    @contextlib.asynccontextmanager
    async def fake_http(url, **kw):
        seen.append(url)
        async with stdio_client(StdioServerParameters(command=sys.executable, args=[FAKE])) as streams:
            yield streams
    monkeypatch.setattr(mcp_manager, "streamable_http_client", fake_http)
    m = Manager({"web": ServerConfig(name="web", transport="http", url="http://127.0.0.1:9911/mcp", risk="network")})
    await m.start()
    try:
        assert seen == ["http://127.0.0.1:9911/mcp"] and m.status()[0].state == "ready"
        assert (registry.get("web_echo").risk, registry.get("web_echo").source) == ("network", "mcp:web")
        assert (await m.call_tool("web", "echo", {"text": "hi"})).text == "hi"
    finally:
        await m.stop()

async def test_start_returns_while_a_server_hangs(fake):
    began = time.monotonic()
    m = await fake(args=[FAKE, "--hang"], wait=0.3)
    assert time.monotonic() - began < 2 and m.status()[0].state == "starting"
    assert not m.available("fake")
    with pytest.raises(ToolError):
        await m.call_tool("fake", "echo", {"text": "x"})
    await m.stop()


async def test_start_reads_init_timeout_when_called(fake, monkeypatch):
    monkeypatch.setattr(mcp_manager, "INIT_TIMEOUT", 0.3)
    began = time.monotonic()
    m = await fake(args=[FAKE, "--hang"])
    assert time.monotonic() - began < 2 and m.status()[0].state == "starting"


async def test_cancelled_call_restarts_a_server_that_stays_busy(fake, monkeypatch):
    monkeypatch.setattr(mcp_manager, "CANCEL_GRACE", 0.2)
    m = await fake()
    task = asyncio.create_task(m.call_tool("fake", "slow", {"seconds": 30}))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await eventually(lambda: m.status()[0].forced_restarts == 1 and m.status()[0].state == "ready")
    assert (await m.call_tool("fake", "echo", {"text": "back"})).text == "back"


async def test_disabled_and_hidden_servers(fake):
    off = await fake(enabled=False)
    assert off.status()[0].state == "disabled" and registry.get("fake_echo") is None
    await off.stop()
    hidden = await fake(expose=False)
    assert registry.get("fake_echo").expose is False and hidden.status()[0].state == "ready"


async def test_http_headers_are_sent_stored_and_never_shown(monkeypatch, isolated_home):
    seen = []
    @contextlib.asynccontextmanager
    async def fake_http(url, **kw):
        seen.append((url, dict(kw["http_client"].headers) if kw.get("http_client") else None))
        async with stdio_client(StdioServerParameters(command=sys.executable, args=[FAKE])) as streams:
            yield streams
    monkeypatch.setattr(mcp_manager, "streamable_http_client", fake_http)
    cfg = mcp_manager.put_server("remote", {"transport": "http", "url": "https://mcp.example.com/mcp", "risk": "network",
                                            "headers": {"Authorization": "Bearer tok_abc"}}, create=True)
    m = Manager({"remote": cfg})
    await m.start()
    try:
        assert seen[0][0] == "https://mcp.example.com/mcp" and seen[0][1]["authorization"] == "Bearer tok_abc"
        assert m.status()[0].state == "ready"
    finally:
        await m.stop()
    kept = mcp_manager.put_server("remote", {"headers": {"Authorization": None, "X-Team": "a"}}, create=False)
    assert kept.headers == {"Authorization": "Bearer tok_abc", "X-Team": "a"}
    from hunterai.api.mcp_admin import _server_view
    view = _server_view(kept)
    assert view["header_names"] == ["Authorization", "X-Team"] and "tok_abc" not in json.dumps(view)


@pytest.mark.parametrize("fields, message", [
    ({"transport": "http", "url": "http://mcp.example.com/mcp", "headers": {"Authorization": "x"}}, "https"),
    ({"transport": "http", "url": "ftp://mcp.example.com/mcp"}, "http"),
    ({"transport": "http", "url": "https://user:pw@mcp.example.com/mcp"}, "user name"),
    ({"transport": "http", "url": "https://mcp.example.com/mcp", "headers": {"Bad Name": "x"}}, "header"),
    ({"transport": "http", "url": "https://mcp.example.com/mcp", "headers": {"X-A": "line\nbreak"}}, "header"),
    ({"command": "node", "headers": {"X-A": "1"}}, "http server"),
])
def test_http_settings_that_would_leak_or_break_are_refused(isolated_home, fields, message):
    with pytest.raises(ValueError, match=message):
        mcp_manager.put_server("remote", fields, create=True)


def test_plain_http_with_a_secret_is_fine_on_this_machine_only(isolated_home):
    cfg = mcp_manager.put_server("local", {"transport": "http", "url": "http://127.0.0.1:9000/mcp", "headers": {"X-Key": "k"}}, create=True)
    assert cfg.headers == {"X-Key": "k"}


def test_a_header_can_name_a_key_of_the_env_file_instead_of_holding_it(tmp_path, monkeypatch):
    env = tmp_path / ".env"; env.write_text("ACME_API_KEY=k-123\nEMPTY_ONE=\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    assert mcp_manager.resolve_headers({"Authorization": "Bearer ${ACME_API_KEY}", "X-Plain": "as it is", "X-Two": "${ACME_API_KEY}:${ACME_API_KEY}"}) == {
        "Authorization": "Bearer k-123", "X-Plain": "as it is", "X-Two": "k-123:k-123"}
    for value in ("Bearer ${MISSING_KEY}", "${EMPTY_ONE}"):
        with pytest.raises(ValueError, match="is not set in the .env file"):
            mcp_manager.resolve_headers({"Authorization": value})
    assert mcp_manager.resolve_headers({"X": "$ACME_API_KEY ${lower} ${}"}) == {"X": "$ACME_API_KEY ${lower} ${}"}      # only ${NAME} is a reference


def test_a_refused_sign_in_is_said_in_words():
    import httpx2
    def status(code):
        r = httpx2.Response(code, request=httpx2.Request("POST", "https://mcp.acme.test/mcp"))
        return httpx2.HTTPStatusError(f"Client error '{code}'", request=r.request, response=r)
    said = mcp_manager.explain(ExceptionGroup("unhandled errors in a TaskGroup", [status(401)]))
    assert "answered 401" in said and "Authorization" in said and "${NAME}" in said and "ExceptionGroup" not in said
    assert "answered 403" in mcp_manager.explain(ExceptionGroup("g", [ExceptionGroup("inner", [status(403)])]))
    assert mcp_manager.explain(ExceptionGroup("g", [status(500)])).startswith("the server answered 500")
    assert mcp_manager.explain(ValueError("header Authorization names ${X}, which is not set in the .env file")) == "header Authorization names ${X}, which is not set in the .env file"
    assert mcp_manager.explain(ExceptionGroup("g", [OSError("boom")])) == "OSError: boom"
    assert mcp_manager.explain(RuntimeError("x" * 500)) == "RuntimeError: " + "x" * 300
    said = mcp_manager.explain(ExceptionGroup("g", [RuntimeError("Server returned an error response")]), 401)   # the library drops the status
    assert "answered 401" in said and "OAuth" in said
    assert mcp_manager.explain(RuntimeError("later failure"), 200) == "RuntimeError: later failure"
