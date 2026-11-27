"""A call that names a host of the target's out-of-scope list is not run, and nobody is asked. Until now such a call
asked the owner each time, with a warning (permissions.decide): on the owner's laptop a 9B model named an out-of-scope
host in all 15 runs of the bench's scope tests, and each of those would have been a prompt to answer. The guard reads
the target's lists and changes nothing in them."""
import asyncio
import dataclasses

import pytest
import respx

from hunterai import mcp_manager, permissions
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import chats, messages, runs, settings
from hunterai.tools import (
    loopguard,
    on_demand,
    registry,
    runner,
    scopeguard,
    search,
    shell,
    web,
)
from hunterai.tools.base import HostUse, Prepared, Tool, ToolResult
from hunterai.types import TextBlock
from tests.test_api import client  # noqa: F401 (client is a fixture)
from tests.test_engine import go, use
from tests.toolkit import Turns, answer_next, collect, fake_tool, seeded_run

SHOP = {"in": ["*.shop.example"], "out": ["payments.shop.example"]}
NET = {"risk": {"network": "allow"}}
OUT = "https://payments.shop.example/v1/charges"
REFUSED = ("Not run: payments.shop.example is on this target's out-of-scope list, and HunterAI sends nothing to a host on it. "
           "Do not call it again. Go on with what is in scope, or say in your answer that this host is out of scope.")


@pytest.fixture(autouse=True)
def tools_registered():
    for t in [*web.tools(), *shell.tools(), *on_demand.tools()]:
        registry.register(t)


async def offered(conn, chat):
    return {t.name for t in await runner.offered_tools(conn, await chats.get(conn, chat.id))}


async def judged(conn, tc, scope=SHOP, policy=None, guard=None, names=None):
    """Runs one call as a loop does: the guard looks at it, then the runner gets it. Returns (outcome, events, steps, guard)."""
    chat, run_id = await seeded_run(conn, scope=scope, permissions=policy or {})
    guard = guard or scopeguard.Guard(loopguard.Guard())
    names = names or await offered(conn, chat)
    publish, events = collect()
    await guard.judge(conn, chat.id, run_id, tc, names)
    out = await asyncio.wait_for(runner.call(conn, chat_id=chat.id, run_id=run_id, tc=tc, offered=names, publish=publish, guard=guard), 10)
    return out, events, (await runs.get(conn, run_id)).steps, guard


def fetch(url=OUT, n=1):
    return ToolCall(f"c{n}", "web_fetch", {"url": url})


# ---- one call

@respx.mock
async def test_a_call_that_names_an_out_of_scope_host_is_not_run_and_nobody_is_asked(conn, dns):
    out, events, steps, _ = await judged(conn, fetch())
    assert (out.block.is_error, out.block.content, out.invalid) == (True, REFUSED, False)
    assert permissions.pending() == [] and not [e for e in events if e["type"] == "permission_request"]
    assert dns[1] == []                                             # not even its name was looked up
    assert not respx.calls
    assert [(s.kind, s.name, s.status) for s in steps] == [("tool_call", "web_fetch", "error")]
    assert steps[0].detail["error"] == REFUSED
    assert [e["type"] for e in events] == ["step_started", "step_finished", "tool_finished"]
    assert (events[2]["content"], events[2]["is_error"]) == (REFUSED, True)


@respx.mock
async def test_with_calls_allowed_for_the_network_it_is_refused_all_the_same(conn, dns):
    out, _, _, _ = await judged(conn, fetch(), policy=NET)
    assert out.block.content == REFUSED and dns[1] == [] and not respx.calls


@respx.mock
async def test_a_host_in_scope_is_fetched_as_before(conn, dns):
    dns[0]["www.shop.example"] = ["93.184.216.34"]
    respx.get("https://93.184.216.34/").respond(200, text="the shop", headers={"content-type": "text/plain"})
    out, _, _, _ = await judged(conn, fetch("https://www.shop.example/"), policy=NET)
    assert out.block.is_error is False and "the shop" in out.block.content


async def test_a_host_on_neither_list_asks_the_owner_as_before(conn, dns):
    task = asyncio.create_task(judged(conn, fetch("https://cdn.other.example/a.js"), policy=NET))
    req = await answer_next(None, "deny")
    out, _, _, _ = await task
    assert any("not in this target's in-scope list" in w for w in req.warnings)
    assert out.block.content.startswith("The owner denied this call.")


async def test_a_chat_without_a_target_has_nothing_to_refuse_by(conn, dns):
    task = asyncio.create_task(judged(conn, fetch(), scope=None))
    req = await answer_next(None, "deny")
    await task
    assert req.warnings == []


async def test_switched_off_the_call_asks_the_owner_with_its_warning_as_before(conn, dns):
    await settings.put(conn, scopeguard.SETTING, False)
    dns[0]["payments.shop.example"] = ["93.184.216.34"]
    task = asyncio.create_task(judged(conn, fetch(), policy=NET))
    req = await answer_next(None, "deny")
    out, _, _, _ = await task
    assert req.warnings == ["payments.shop.example is on this target's out-of-scope list"]
    assert out.block.content.startswith("The owner denied this call.")


def test_it_is_on_unless_the_owner_switches_it_off():
    assert (scopeguard.SETTING, scopeguard.DEFAULT) == ("scope_refusal", True)


async def test_a_command_that_names_the_host_is_not_run(conn, dns, tmp_path):
    out, _, steps, _ = await judged(conn, ToolCall("c1", "execute", {"command": f"curl -s {OUT} -o {tmp_path}/got"}))
    assert out.block.content == REFUSED and not (tmp_path / "got").exists()
    assert [(s.kind, s.status) for s in steps] == [("tool_call", "error")]


async def test_an_address_on_the_out_of_scope_list_is_refused_without_a_lookup(conn, dns):
    out, _, _, _ = await judged(conn, fetch("http://10.20.30.40/admin"), scope={"in": ["10.20.30.0/24"], "out": ["10.20.30.40"]})
    assert out.block.content == REFUSED.replace("payments.shop.example", "10.20.30.40") and dns[1] == []


async def test_a_name_is_not_looked_up_to_see_whether_its_address_is_out_of_scope(conn, dns):
    """A lookup tells the name to a name server before the owner allowed anything (web tools, Ruling 19). The runner
    looks it up once the call may go on, and asks with the warning then, as before."""
    dns[0]["api.shop.example"] = ["10.20.30.40"]
    task = asyncio.create_task(judged(conn, fetch("http://api.shop.example/"), scope={"in": ["*.shop.example"], "out": ["10.20.30.40"]}))
    req = await answer_next(None, "deny")
    out, _, _, _ = await task
    assert out.block.content.startswith("The owner denied this call.") and req.tool == "web_fetch"


async def test_two_hosts_are_both_named(conn, dns):
    scope = {"in": ["*.shop.example"], "out": ["payments.shop.example", "pay.shop.example"]}
    out, _, _, _ = await judged(conn, ToolCall("c1", "execute", {"command": f"curl {OUT} https://pay.shop.example/"}), scope=scope)
    assert out.block.content == ("Not run: payments.shop.example and pay.shop.example are on this target's out-of-scope list, and HunterAI "
                                 "sends nothing to a host on it. Do not call them again. Go on with what is in scope, or say in your "
                                 "answer that these hosts are out of scope.")


# ---- a line with a path: only an address under it is out of scope

BLOG = {"in": ["www.shop.example"], "out": ["www.shop.example/blog"]}
UNDER = ("Not run: this call goes to an address on this target's out-of-scope list (www.shop.example: the line www.shop.example/blog), "
         "and HunterAI sends nothing there. Do not call that address again; other addresses of that host are judged for themselves. "
         "Go on with what is in scope, or say in your answer that this address is out of scope.")


async def test_a_fetch_under_an_out_of_scope_path_is_refused_and_the_model_is_told_it_is_that_address(conn, dns):
    out, _, _, _ = await judged(conn, fetch("https://www.shop.example/blog/2026/post"), scope=BLOG, policy=NET)
    assert out.block.content == UNDER and dns[1] == []
    out, _, _, _ = await judged(conn, fetch("https://www.shop.example/BLOG//drafts"), scope=BLOG, policy=NET)
    assert out.block.content == UNDER                               # a near miss is out too, as the warning has it


@respx.mock
async def test_another_path_of_that_host_is_fetched_as_before(conn, dns):
    dns[0]["www.shop.example"] = ["93.184.216.34"]
    respx.get("https://93.184.216.34/api/users").respond(200, text="the users", headers={"content-type": "text/plain"})
    out, _, _, _ = await judged(conn, fetch("https://www.shop.example/api/users"), scope=BLOG, policy=NET)
    assert out.block.is_error is False and "the users" in out.block.content


async def test_a_command_naming_a_host_that_only_a_line_with_a_path_excludes_is_the_owners_to_answer(conn, dns):
    """A command hands the check no path, so a line with a path counts for the whole host there and the call asks
    (README, "Checking one host"). Whether the command goes under that path the guard cannot tell, so it refuses
    nothing: the host as a whole is not out of scope, and the model must not be told that it is."""
    task = asyncio.create_task(judged(conn, ToolCall("c1", "execute", {"command": "curl -s https://www.shop.example/api/users"}), scope=BLOG))
    req = await answer_next(None, "deny")
    out, _, _, guard = await task
    assert req.warnings == ["www.shop.example is on this target's out-of-scope list"]
    assert out.block.content.startswith("The owner denied this call.") and guard.stuck() is None


async def test_the_line_that_is_named_is_the_one_with_the_path(conn, dns):
    """`*.shop.example` is out and `www.shop.example` is named in scope, so the wildcard does not exclude it (README,
    "Which list wins"). Under /blog it is out, by the line with the path: that one is named, not the wildcard."""
    scope = {"in": ["www.shop.example"], "out": ["*.shop.example", "www.shop.example/blog"]}
    out, _, _, _ = await judged(conn, fetch("https://www.shop.example/blog/2026/post"), scope=scope, policy=NET)
    assert out.block.content == UNDER


async def test_a_host_that_is_out_as_a_whole_and_under_a_path_is_named_as_a_whole(conn, dns):
    scope = {"in": ["*.shop.example"], "out": ["payments.shop.example/v1", "payments.shop.example"]}
    out, _, _, _ = await judged(conn, fetch(), scope=scope)
    assert out.block.content == REFUSED


def test_the_word_for_several_addresses_names_each_with_its_line():
    assert scopeguard.refusal([("a.example", "a.example/admin"), ("b.example", None)]) == (
        "Not run: this call goes to an address on this target's out-of-scope list (a.example: the line a.example/admin; b.example), "
        "and HunterAI sends nothing there. Do not call that address again; other addresses of that host are judged for themselves. "
        "Go on with what is in scope, or say in your answer that this address is out of scope.")


async def test_a_tool_loaded_on_demand_is_judged_as_the_tool_it_names(conn, dns):
    tool, ran = fake_tool("srv_send", risk="network", source="mcp:srv", prepared=Prepared(hosts=(HostUse("payments.shop.example", 443),)))
    registry.register(tool)
    call = ToolCall("c1", on_demand.CALL, {"tool": "srv_send", "arguments": {"path": "/v1/charges"}})
    out, _, steps, _ = await judged(conn, call, names={"srv_send", on_demand.CALL, on_demand.FIND})
    assert out.block.content == REFUSED and ran == []
    assert [(s.kind, s.name, s.status) for s in steps] == [("tool_call", "srv_send", "error")]


# ---- a tool of an MCP server: HunterAI reads whole addresses off its arguments, and the guard every host besides

def a_scanner(risk="execute") -> list:
    """A tool of an MCP server as the manager registers one: what a call touches is read off its arguments, the same
    way for every server (mcp_manager.generic_prepare). Returns what it was run with."""
    ran: list = []

    async def handler(ctx, args):
        ran.append(args)
        return ToolResult("scanned")

    async def prepare(ctx, args):
        return await mcp_manager.generic_prepare(risk, ctx, args)
    registry.register(Tool("srv_scan", "scans a host", {"type": "object"}, risk, handler, prepare=prepare, source="mcp:scanner"))
    return ran


async def named(conn, name: str, arguments: dict, scope=SHOP, names=None) -> list:
    """What the guard would refuse this call for."""
    chat, run_id = await seeded_run(conn, scope=scope)
    return await scopeguard.named(conn, chat.id, run_id, ToolCall("c1", name, arguments), names or {name})


async def test_a_tool_of_a_server_that_is_given_the_host_as_a_bare_name_is_not_run(conn, dns):
    """Found on 27 November 2026: HunterAI reads whole addresses off the arguments of a server's tool and no bare name,
    so a scanner that was given `payments.shop.example` named no host for the guard, and a chat ran it."""
    ran = a_scanner()
    call = ToolCall("c1", "srv_scan", {"target": "payments.shop.example", "ports": "1-1024"})
    out, events, steps, _ = await judged(conn, call, names={"srv_scan"})
    assert (out.block.is_error, out.block.content, ran) == (True, REFUSED, [])
    assert permissions.pending() == [] and not [e for e in events if e["type"] == "permission_request"]
    assert dns[1] == []
    assert [(s.kind, s.name, s.status) for s in steps] == [("tool_call", "srv_scan", "error")]


@pytest.mark.parametrize("arguments", [
    {"target": "payments.shop.example"},
    {"targets": ["www.shop.example", "payments.shop.example"]},
    {"options": {"scan": {"host": "payments.shop.example", "port": 443}}},
    {"command": "scan --host payments.shop.example --fast"},
    {"target": "payments.shop.example:8443"},
])
async def test_the_name_is_found_wherever_it_stands_among_the_arguments(conn, dns, arguments):
    a_scanner()
    assert await named(conn, "srv_scan", arguments) == [("payments.shop.example", None)]


async def test_a_name_in_scope_a_name_on_neither_list_and_a_word_that_only_looks_like_a_host_are_left_alone(conn, dns):
    a_scanner()
    for arguments in ({"target": "www.shop.example"}, {"target": "cdn.other.example"}, {"file": "README.md", "notes": "see scan.txt"}):
        assert await named(conn, "srv_scan", arguments) == [], arguments


async def test_an_address_and_a_bare_name_are_each_named_once_the_address_first(conn, dns):
    a_scanner()
    scope = {"in": ["*.shop.example"], "out": ["payments.shop.example", "vault.shop.example"]}
    arguments = {"also": "vault.shop.example and payments.shop.example", "url": "https://payments.shop.example/login"}
    assert await named(conn, "srv_scan", arguments, scope=scope) == [("payments.shop.example", None), ("vault.shop.example", None)]


async def test_a_bare_name_that_only_a_line_with_a_path_excludes_is_not_refused(conn, dns):
    """A bare name says no path, as a command says none: it cannot be told whether the call goes under that path."""
    a_scanner()
    scope = {"in": ["*.shop.example"], "out": ["www.shop.example/admin"]}
    assert await named(conn, "srv_scan", {"target": "www.shop.example"}, scope=scope) == []
    under = await named(conn, "srv_scan", {"url": "https://www.shop.example/admin/users"}, scope=scope)
    assert under == [("www.shop.example", "www.shop.example/admin")]             # the one address it names is judged as before


async def test_a_bare_address_in_an_excluded_network_is_refused_too(conn, dns):
    a_scanner()
    assert await named(conn, "srv_scan", {"target": "10.1.2.3"}, scope={"in": [], "out": ["10.0.0.0/8"]}) == [("10.1.2.3", None)]
    assert await named(conn, "srv_scan", {"target": "192.168.1.5"}, scope={"in": [], "out": ["10.0.0.0/8"]}) == []


async def test_a_tool_of_a_server_that_only_reads_is_not_looked_at_as_before(conn, dns):
    a_scanner(risk="read")
    assert await named(conn, "srv_scan", {"target": "payments.shop.example", "url": "https://payments.shop.example/"}) == []


async def test_a_tool_of_a_server_that_is_loaded_on_demand_is_refused_the_bare_name_too(conn, dns):
    ran = a_scanner()
    call = ToolCall("c1", on_demand.CALL, {"tool": "srv_scan", "arguments": {"target": "payments.shop.example"}})
    out, _, steps, _ = await judged(conn, call, names={"srv_scan", on_demand.CALL, on_demand.FIND})
    assert (out.block.content, ran) == (REFUSED, [])
    assert [(s.kind, s.name, s.status) for s in steps] == [("tool_call", "srv_scan", "error")]


async def test_a_tool_of_hunterais_own_is_read_as_it_says_itself_and_no_further(conn, dns):
    """A web search about the host goes to a search engine, and its tool says that scope does not apply (tools/search.py).
    A fetch goes to the host of its address. Neither is read for names, as a tool of a server is."""
    for t in search.tools():
        registry.register(t)
    assert await named(conn, "web_search", {"query": "payments.shop.example login rate limit"}) == []
    assert await named(conn, "web_fetch", {"url": "https://www.shop.example/go?next=payments.shop.example"}) == []


async def test_a_call_the_runner_will_not_take_is_left_to_the_runner(conn, dns):
    for call, begins in ((ToolCall("c1", "no_such_tool", {"url": OUT}), 'Unknown tool "no_such_tool".'),
                         (ToolCall("c2", "web_fetch", {"link": OUT}), "Invalid arguments for web_fetch:"),
                         (ToolCall("c3", "web_fetch", {}, error="not JSON"), "Invalid call to web_fetch: not JSON."),
                         (ToolCall("c4", "web_fetch", {"url": "https://payments.shop.example:99999/"}), "")):
        out, _, _, guard = await judged(conn, call)
        assert out.block.is_error and out.block.content.startswith(begins) and out.block.content != REFUSED
        assert guard.stuck() is None


async def test_what_the_runner_would_not_take_from_a_tool_that_takes_anything_is_left_to_it_too(conn, dns):
    """The runner asks a loop's guard before it looks at the tool and its arguments. A refusal for a call it would
    turn away for another reason would stand in place of that reason."""
    reaches = Prepared(hosts=(HostUse("payments.shop.example", 443),))
    strict = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}
    tool, ran = fake_tool("t_strict", risk="network", schema=strict, prepared=reaches)
    registry.register(tool)
    registry.register(dataclasses.replace(fake_tool("t_hidden", risk="network", prepared=reaches)[0], expose=False))
    registry.register(fake_tool("t_elsewhere", risk="network", prepared=reaches)[0])
    names = {"t_strict", "t_hidden", on_demand.CALL, on_demand.FIND}
    for call, begins in ((ToolCall("c1", "t_strict", {"url": 5}), "Invalid arguments for t_strict:"),
                         (ToolCall("c2", "t_hidden", {}), 'Unknown tool "t_hidden".'),
                         (ToolCall("c3", "t_elsewhere", {}), 'Unknown tool "t_elsewhere".'),
                         (ToolCall("c4", on_demand.CALL, {"tool": "t_strict", "arguments": {"url": OUT}}), '"t_strict" is not a tool that is loaded on demand.')):
        out, _, _, guard = await judged(conn, call, names=names)
        assert out.block.content.startswith(begins), (call.name, out.block.content)
        assert guard.stuck() is None
    out, _, _, _ = await judged(conn, ToolCall("c5", "t_strict", {"url": OUT}), names=names)
    assert out.block.content == REFUSED and ran == []              # and the same tool, asked as it wants to be, is refused


async def test_mcp_call_is_only_the_tool_it_names_where_mcp_call_is_offered(conn, dns):
    tool, ran = fake_tool("srv_send", risk="network", source="mcp:srv", prepared=Prepared(hosts=(HostUse("payments.shop.example", 443),)))
    registry.register(tool)
    call = ToolCall("c1", on_demand.CALL, {"tool": "srv_send", "arguments": {}})
    out, _, _, _ = await judged(conn, call, names={"srv_send"})
    assert out.block.content.startswith('Unknown tool "mcp_call".') and ran == []


async def test_only_a_tool_that_reaches_out_is_looked_at_before_the_runner_does(conn, dns):
    seen = []

    def counting(risk):
        tool, _ = fake_tool(f"t_{risk}", risk=risk)

        async def prepare(ctx, args):
            seen.append(risk)
            return Prepared()
        return dataclasses.replace(tool, prepare=prepare)
    for risk in ("read", "write", "network", "execute"):
        registry.register(counting(risk))
    chat, run_id = await seeded_run(conn, scope=SHOP)
    guard = scopeguard.Guard(loopguard.Guard())
    for n, risk in enumerate(("read", "write", "network", "execute")):
        await guard.judge(conn, chat.id, run_id, ToolCall(f"c{n}", f"t_{risk}", {}), {f"t_{risk}"})
    assert seen == ["network", "execute"]


# ---- the setting

async def test_the_setting_is_on_for_a_new_install_and_can_be_switched(client):
    assert (await client.get("/api/settings")).json()["scope_refusal"] is True
    assert (await client.put("/api/settings", json={"scope_refusal": False})).json()["scope_refusal"] is False
    assert (await client.get("/api/settings")).json()["scope_refusal"] is False
    assert (await client.put("/api/settings", json={"max_turns": 30})).json()["scope_refusal"] is False      # another setting leaves it
    assert (await client.put("/api/settings", json={"scope_refusal": True})).json()["scope_refusal"] is True
    assert (await client.put("/api/settings", json={"scope_refusal": "sometimes"})).status_code == 422


# ---- many calls

def test_three_refusals_in_a_row_and_the_model_is_taken_to_be_stuck():
    g = scopeguard.Guard(loopguard.Guard())
    for n in (1, 2):
        g.refuse(fetch(n=n), [("payments.shop.example", None)])
        assert g.before(fetch(n=n)) == REFUSED and g.stuck() is None
    g.refuse(fetch("https://payments.shop.example/other", n=3), [("payments.shop.example", None)])
    assert g.before(fetch("https://payments.shop.example/other", n=3)) == REFUSED
    assert g.stuck() == ("Stopped: the model named a host of this target's out-of-scope list in 3 calls in a row "
                         "(payments.shop.example), and none of them was run. Tell it what to do instead, or give the task to "
                         "another model.")


def test_a_call_that_is_not_refused_starts_the_count_again():
    g = scopeguard.Guard(loopguard.Guard())
    for n in (1, 2):
        g.refuse(fetch(n=n), [("payments.shop.example", None)])
        g.before(fetch(n=n))
    assert g.before(fetch("https://www.shop.example/", n=3)) is None
    g.refuse(fetch(n=4), [("payments.shop.example", None)])
    g.before(fetch(n=4))
    assert g.stuck() is None


def test_what_the_loops_own_guard_says_is_passed_on():
    inner = loopguard.Guard()
    g = scopeguard.Guard(inner)
    call = {"url": "https://www.shop.example/a"}
    for n in (1, 2):
        assert g.before(ToolCall(f"c{n}", "web_fetch", call)) is None
        assert g.after(ToolCall(f"c{n}", "web_fetch", call), "HTTP 503 https://www.shop.example/a\n\ndown", False) == ""
    for n in (3, 4, 5):
        assert "2 times" in g.before(ToolCall(f"c{n}", "web_fetch", call))
    assert g.stuck() == inner.stuck() and g.stuck().startswith("Stopped: the model kept sending a call that could not work")


def test_a_refusal_is_for_the_call_it_was_made_for():
    g = scopeguard.Guard(loopguard.Guard())
    g.refuse(fetch(n=1), [("payments.shop.example", None)])
    assert g.before(fetch("https://www.shop.example/", n=2)) is None       # another call is not refused in its place
    assert g.before(fetch(n=1)) == REFUSED


# ---- a run

async def test_a_run_goes_on_after_the_refusal_and_the_tool_is_never_run(conn, dns):
    tool, ran = fake_tool("t_send", risk="network", schema={"type": "object", "properties": {"url": {"type": "string"}}},
                          prepared=Prepared(hosts=(HostUse("payments.shop.example", 443),)))
    registry.register(tool)
    p = Turns("abliteration", [ToolCall("c1", "t_send", {"url": OUT})], [TextDelta("payments.shop.example is out of scope, so I left it alone.")])
    use(abliteration=p)
    chat, _ = await seeded_run(conn, scope=SHOP)
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="test the payment API")])
    run, msg = await asyncio.wait_for(go(conn, chat, user), 20)         # without the guard the call asks, and nobody answers
    assert (run.status, run.error, ran) == ("done", None, [])
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_result", "text"]
    assert (msg.blocks[1].is_error, msg.blocks[1].content) == (True, REFUSED)
    assert not [s for s in run.steps if s.kind == "permission"]
    sent = p.calls[1]["messages"][-1].blocks                        # the model reads the refusal as the result of its call
    assert [b.content for b in sent if b.type == "tool_result"] == [REFUSED]


async def test_a_run_is_stopped_when_the_model_names_the_host_three_times_in_a_row(conn, dns):
    tool, ran = fake_tool("t_send", risk="network", schema={"type": "object", "properties": {"url": {"type": "string"}}},
                          prepared=Prepared(hosts=(HostUse("payments.shop.example", 443),)))
    registry.register(tool)
    p = Turns("abliteration", *[[ToolCall(f"c{n}", "t_send", {"url": f"{OUT}?try={n}"})] for n in range(6)])
    use(abliteration=p)
    chat, _ = await seeded_run(conn, scope=SHOP)
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="test the payment API")])
    run, msg = await asyncio.wait_for(go(conn, chat, user), 20)         # without the guard the call asks, and nobody answers
    assert ran == [] and len(p.calls) == 3
    assert run.error.startswith("Stopped: the model named a host of this target's out-of-scope list in 3 calls in a row")
    assert [b.content for b in msg.blocks if b.type == "tool_result"] == [REFUSED] * 3
