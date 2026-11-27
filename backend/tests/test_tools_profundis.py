import asyncio
import json

import httpx
import pytest
import respx

from hunterai.providers.base import ToolCall
from hunterai.tools import profundis, registry, runner
from hunterai.repo import chats
from tests.toolkit import answer_next, run_call, seeded_run

KEY = "pk_testkeyid123_" + "s" * 43
API = "https://api.profundis.io/api/v2/common/data"


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    monkeypatch.setenv("PROFUNDIS_API_KEY", KEY)
    for t in profundis.tools():
        registry.register(t)


async def use(conn, **args):
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c", "profundis", args)))
    req = await answer_next(run_id, "allow")
    out, _, _ = await task
    return out.block, req


async def test_it_is_offered_only_while_a_key_is_set(conn, monkeypatch, tmp_path):
    chat, _ = await seeded_run(conn)
    assert "profundis" in {t.name for t in await runner.offered_tools(conn, await chats.get(conn, chat.id))}
    monkeypatch.delenv("PROFUNDIS_API_KEY")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(tmp_path / "none.env"))
    assert "profundis" not in {t.name for t in await runner.offered_tools(conn, await chats.get(conn, chat.id))}


@respx.mock
async def test_host_search_sends_the_key_in_a_header_and_lists_rows(conn):
    route = respx.post(f"{API}/hosts").mock(return_value=httpx.Response(200, json={
        "total": 2, "took": 4, "relation": "eq", "next_after": "abc",
        "data": [{"host": "a.example.com", "port": 443, "resolution": "192.0.2.1", "title": "Portal", "id": "x1",
                  "technologies": ["nginx"], "headers": {"server": "nginx"}, "favicon_hashes": []},
                 {"host": "b.example.com", "port": 80, "status_code": 301}]}))
    block, req = await use(conn, action="hosts", query="host:*.example.com", per_page=50, page=2)
    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == KEY and "authorization" not in sent.headers and KEY not in str(sent.url)
    assert json.loads(sent.content) == {"raw_query": "host:*.example.com", "aggregate": False, "results_per_page": 50, "page": 2}
    text = block.content
    assert not block.is_error and "2 results" in text and "page 2" in text
    assert '"host": "a.example.com"' in text and '"port": 443' in text and '"b.example.com"' in text
    assert "favicon_hashes" not in text                                 # empty values are left out
    assert KEY not in text and KEY not in (req.preview or "")
    assert "1 credit" in (req.preview or "") and req.grant_target == "profundis:hosts"


@respx.mock
async def test_fields_and_default_page_size(conn):
    route = respx.post(f"{API}/dns").mock(return_value=httpx.Response(200, json={"total": 0, "took": 1, "relation": "eq", "data": []}))
    block, _ = await use(conn, action="dns", query="value:192.0.2.1", fields="host,value")
    assert json.loads(route.calls.last.request.content) == {
        "raw_query": "value:192.0.2.1", "aggregate": False, "results_per_page": 10, "page": 1, "include": "host,value"}
    assert block.content.startswith("0 results")


@pytest.mark.parametrize("args, message", [
    ({"action": "hosts"}, "needs a query"),
    ({"action": "hosts", "query": "h" * 513}, "512 bytes"),
    ({"action": "subdomains"}, "needs a domain"),
    ({"action": "domain", "domain": "not a domain"}, "not a domain name"),
    ({"action": "subdomains", "domain": "http://example.com/x"}, "not a domain name"),
])
async def test_bad_arguments_are_refused_before_anything_is_asked(conn, args, message):
    chat, run_id = await seeded_run(conn)
    out, events, _ = await run_call(conn, chat, run_id, ToolCall("c", "profundis", args))
    assert out.block.is_error and message in out.block.content
    assert all(e["type"] != "permission_request" for e in events)


@respx.mock
async def test_subdomains_reads_names_and_control_lines_and_caps_credits(conn):
    """The limit a call has when the model names none is what the first names of a domain cost: Profundis charges 10
    credits for up to 100 of them, and under a lower limit it sends no name at all."""
    body = "a.example.com\nb.example.com\n" + json.dumps({"type": "credits", "credits_charged": 10, "capped": False, "max_credits": 10}) + "\n"
    route = respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    block, req = await use(conn, action="subdomains", domain="Example.com.")
    assert json.loads(route.calls.last.request.content) == {"domain": "example.com", "max_credits": 10}
    assert "2 names" in block.content and "a.example.com\nb.example.com" in block.content and "credits_charged" in block.content
    assert "Cost: at most 10 credits of your Profundis account (10 for the first 100 names, 1 for each 50 more)" in req.preview
    assert req.grant_target == "profundis:subdomains:10"
    _, req = await use(conn, action="subdomains", domain="example.com", max_credits=11)
    assert json.loads(route.calls.last.request.content) == {"domain": "example.com", "max_credits": 11}
    assert "at most 11 credits" in req.preview and req.grant_target == "profundis:subdomains:11"      # a higher limit asks again


async def test_a_limit_that_buys_no_name_is_refused_before_anything_is_asked(conn):
    """A model that read "0 names" under such a limit would report a domain without subdomains."""
    tool = profundis.tools()[0]
    limit = tool.parameters["properties"]["max_credits"]
    assert limit["minimum"] == profundis.DEFAULT_MAX_CREDITS == 10 and "no name" in limit["description"]
    chat, run_id = await seeded_run(conn)
    out, events, _ = await run_call(conn, chat, run_id, ToolCall("c", "profundis", {"action": "subdomains", "domain": "example.com", "max_credits": 9}))
    assert out.block.is_error and "9 is less than the minimum of 10" in out.block.content
    assert all(e["type"] != "permission_request" for e in events)


@respx.mock
async def test_what_only_keeps_a_stream_open_is_left_out_and_the_account_lines_are_kept(conn):
    body = "".join(line + "\n" for line in (
        "a.example.com", json.dumps({"type": "heartbeat"}), json.dumps({"type": "heartbeat"}), "b.example.com",
        json.dumps({"type": "enumeration", "only_resolving": False, "partial": True}),
        json.dumps({"type": "credits", "credits_charged": 10, "balance": 90})))
    respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text=body))
    block, _ = await use(conn, action="subdomains", domain="example.com")
    assert not block.is_error and block.content.startswith("2 names") and "heartbeat" not in block.content
    assert '"partial": true' in block.content and '"balance": 90' in block.content


@respx.mock
async def test_a_coded_line_inside_a_subdomain_stream_is_an_error(conn):
    """How the API description writes one: a code and a message, with no `error` around them. Q2014 (the credits ran
    out) can come after the names, and the names before it are then not all there are."""
    body = "a.example.com\n" + json.dumps({"type": "credits", "credits_charged": 10}) + "\n" + json.dumps({"code": "Q2014", "message": "insufficient credits"}) + "\n"
    respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text=body))
    block, _ = await use(conn, action="subdomains", domain="example.com")
    assert block.is_error and "Q2014" in block.content and "insufficient credits" in block.content and "a.example.com" in block.content
    assert block.content.index("credits_charged") < block.content.index("Profundis reported an error") < block.content.index("Q2014")


@pytest.mark.parametrize("line, said", [
    ({"code": "N1", "notice": "the list was stopped at your limit"}, "the list was stopped at your limit"),
    ({"type": "enumeration", "message": "a refresh of the names was queued"}, "a refresh of the names was queued"),
    ({"type": "credits", "code": "C1", "balance": 4}, '"balance": 4'),
])
@respx.mock
async def test_a_line_that_only_tells_something_is_shown_and_is_no_error(conn, line, said):
    """An error is what the API description calls one: something under `error`, or a code together with a message."""
    respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text="a.example.com\n" + json.dumps(line) + "\n"))
    block, _ = await use(conn, action="subdomains", domain="example.com")
    assert not block.is_error and said in block.content and "reported an error" not in block.content


@respx.mock
async def test_an_error_line_that_names_its_kind_is_an_error_too(conn):
    body = "a.example.com\n" + json.dumps({"type": "error", "code": "Q2014", "message": "insufficient credits"}) + "\n"
    respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text=body))
    block, _ = await use(conn, action="subdomains", domain="example.com")
    assert block.is_error and "Q2014" in block.content


@pytest.mark.parametrize("action, page, cost, grant", [
    ("hosts", 1, "Cost: 1 credit of", "profundis:hosts"),
    ("hosts", 3, "Cost: 1 credit of", "profundis:hosts"),
    ("hosts", 4, "Cost: 2 credits of", "profundis:hosts:+1"),
    ("dns", 13, "Cost: 2 credits of", "profundis:dns:+1"),
    ("vhosts", 14, "Cost: 3 credits of", "profundis:vhosts:+2"),
    ("hosts", 100, "Cost: 11 credits of", "profundis:hosts:+10"),
    ("whois", 3, "Cost: 3 credits or more of", "profundis:whois"),
    ("whois", 4, "Cost: 4 credits or more of", "profundis:whois:+1"),
])
@respx.mock
async def test_a_deep_page_costs_more_and_is_asked_for_again(conn, action, page, cost, grant):
    """Pages 1 to 3 cost the price of the search, and each block of ten pages after them one credit more. An answer
    for the chat covers calls that cost what was shown, so a deeper page is not covered by it."""
    respx.post(f"{API}/{action}").mock(return_value=httpx.Response(200, json={"total": 0, "relation": "eq", "data": []}))
    _, req = await use(conn, action=action, query="host:example.com", page=page)
    assert cost in req.preview and req.grant_target == grant


@respx.mock
async def test_an_error_inside_a_subdomain_stream_is_an_error(conn):
    body = "a.example.com\n" + json.dumps({"error": {"code": "QUOTA", "message": "balance exhausted"}}) + "\n"
    respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text=body))
    block, _ = await use(conn, action="subdomains", domain="example.com", max_credits=50)
    assert block.is_error and "balance exhausted" in block.content and "a.example.com" in block.content


@respx.mock
async def test_too_many_names_are_cut_with_a_note(conn, monkeypatch):
    monkeypatch.setattr(profundis, "MAX_NAMES", 3)
    respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text="".join(f"h{i}.example.com\n" for i in range(10))))
    block, _ = await use(conn, action="subdomains", domain="example.com")
    assert block.content.count(".example.com") == 3 and "stopped at 3 names" in block.content


@respx.mock
async def test_domain_and_asn_lookups(conn):
    d = respx.post(f"{API}/domains/details").mock(return_value=httpx.Response(200, json={"domain": "example.com", "total_ips": 1, "results": [{"ip": "192.0.2.1"}]}))
    a = respx.post(f"{API}/asn/details").mock(return_value=httpx.Response(200, json={"profundis_quotas": {"MaxQueryPerDay": 6000}, "results": [{"as_hierarchy": [{"as_number": 64500}]}]}))
    block, req = await use(conn, action="domain", domain="example.com")
    assert json.loads(d.calls.last.request.content) == {"domain": "example.com"} and "192.0.2.1" in block.content
    assert "Cost: 1 credit and 1 more for each address found, 26 at most, of your" in req.preview and not req.grant_target
    for query, body in (("AS64500", {"question": "number", "number": 64500}), ("192.0.2.0/24", {"question": "cidr", "cidr": "192.0.2.0/24"}),
                        ("192.0.2.9", {"question": "ip", "ip": "192.0.2.9"}), ("Example Net", {"question": "partial_name", "partial_name": "Example Net"})):
        block, req = await use(conn, action="asn", query=query)
        assert json.loads(a.calls.last.request.content) == body and query in req.preview
        assert "Cost: 1 credit of your Profundis account" in req.preview and req.grant_target == "profundis:asn"
    assert "64500" in block.content and "MaxQueryPerDay" not in block.content


@pytest.mark.parametrize("status, body, headers, message", [
    (401, {"error": {"code": "UNAUTHORIZED", "message": "bad key " + KEY}}, {}, "did not accept the key"),
    (402, {"error": {"code": "NO_CREDITS", "message": "no credits left"}}, {}, "no credits left"),
    (402, {"code": "Q2014", "message": "fewer than 10 spendable credits"}, {}, "(HTTP 402): Q2014 fewer than 10 spendable credits"),
    (402, ["not", "an", "object"], {}, "(HTTP 402): plan, credits, or budget"),
    (403, {"error": {"code": "PLAN", "message": "paid plan required"}}, {}, "paid plan required"),
    (429, {}, {"retry-after": "7"}, "wait 7 s"),
    (500, None, {}, "HTTP 500"),
])
@respx.mock
async def test_failures_are_plain_and_never_show_the_key(conn, status, body, headers, message):
    resp = httpx.Response(status, json=body, headers=headers) if body is not None else httpx.Response(status, text="<html>oops " + KEY)
    respx.post(f"{API}/hosts").mock(return_value=resp)
    block, _ = await use(conn, action="hosts", query="host:example.com")
    assert block.is_error and message in block.content and KEY not in block.content


@respx.mock
async def test_a_long_answer_is_cut(conn, monkeypatch):
    monkeypatch.setattr(profundis, "MAX_CHARS", 500)
    rows = [{"host": f"h{i}.example.com", "title": "t" * 100} for i in range(50)]
    respx.post(f"{API}/hosts").mock(return_value=httpx.Response(200, json={"total": 50, "took": 1, "relation": "eq", "data": rows}))
    block, _ = await use(conn, action="hosts", query="host:*.example.com")
    assert len(block.content) < 900 and "cut" in block.content


@respx.mock
async def test_no_connection_is_a_plain_error(conn):
    respx.post(f"{API}/hosts").mock(side_effect=httpx.ConnectError("boom"))
    block, _ = await use(conn, action="hosts", query="host:example.com")
    assert block.is_error and "could not reach Profundis" in block.content
