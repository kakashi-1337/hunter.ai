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
    body = "a.example.com\nb.example.com\n" + json.dumps({"credits_charged": 1, "capped": False, "max_credits": 3}) + "\n"
    route = respx.post(f"{API}/subdomains").mock(return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    block, req = await use(conn, action="subdomains", domain="Example.com.")
    assert json.loads(route.calls.last.request.content) == {"domain": "example.com", "max_credits": 3}
    assert "2 names" in block.content and "a.example.com\nb.example.com" in block.content and "credits_charged" in block.content
    assert "at most 3 credits" in req.preview


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
    block, _ = await use(conn, action="domain", domain="example.com")
    assert json.loads(d.calls.last.request.content) == {"domain": "example.com"} and "192.0.2.1" in block.content
    for query, body in (("AS64500", {"question": "number", "number": 64500}), ("192.0.2.0/24", {"question": "cidr", "cidr": "192.0.2.0/24"}),
                        ("192.0.2.9", {"question": "ip", "ip": "192.0.2.9"}), ("Example Net", {"question": "partial_name", "partial_name": "Example Net"})):
        block, req = await use(conn, action="asn", query=query)
        assert json.loads(a.calls.last.request.content) == body and query in req.preview
    assert "64500" in block.content and "MaxQueryPerDay" not in block.content


@pytest.mark.parametrize("status, body, headers, message", [
    (401, {"error": {"code": "UNAUTHORIZED", "message": "bad key " + KEY}}, {}, "did not accept the key"),
    (402, {"error": {"code": "NO_CREDITS", "message": "no credits left"}}, {}, "no credits left"),
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
