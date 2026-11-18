import base64
import gzip
import json

import httpx
import pytest
import respx

from hunterai import caido
from hunterai.providers.base import ToolCall
from hunterai.tools import caido_tools, registry
from hunterai.tools.base import ToolError
from tests.toolkit import run_call, seeded_run

URL = "http://127.0.0.1:8791"
NET = {"risk": {"network": "allow"}}
ROW = {"id": "7", "host": "app.acme.test", "port": 443, "method": "POST", "path": "/api/login", "query": "next=1", "isTls": True,
       "source": "INTERCEPT", "createdAt": 1, "response": {"id": "7", "statusCode": 403, "length": 120, "roundtripTime": 35}}
REQ = b"POST /api/login?next=1 HTTP/1.1\r\nHost: app.acme.test\r\nContent-Type: application/json\r\n\r\n{\"user\":\"a\"}"
RESP = b"HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\n\r\n{\"error\":\"no\"}"


def b64(data):
    return base64.b64encode(data).decode()


class FakeCaido:
    """Answers Caido's GraphQL by what the query asks for."""

    def __init__(self):
        self.up, self.calls, self.tokens, self.projects, self.current = True, [], ["t1", "t2", "t3"], [], None
        self.valid: str | None = None
        self.replays: list[dict] = []
        self.polls = 0
        self.upstreams: list[dict] = []

    def root(self, request):
        if not self.up:
            raise httpx.ConnectError("refused")
        return httpx.Response(200, text="ui")

    def graphql(self, request):
        if not self.up:
            raise httpx.ConnectError("refused")
        body = json.loads(request.content)
        q, v = body["query"], body.get("variables") or {}
        name = next(n for n in ("loginAsGuest", "createProject", "selectProject", "createUpstreamProxyHttp", "deleteUpstreamProxyHttp",
                                "upstreamProxiesHttp", "createReplaySession", "startReplayTask", "replayEntry",
                                "runtime", "currentProject", "request(id", "requestsByOffset") if n in q)
        self.calls.append(name)
        if name == "loginAsGuest":
            self.valid = self.tokens.pop(0)
            return httpx.Response(200, json={"data": {"loginAsGuest": {"token": {"accessToken": self.valid}, "error": None}}})
        if request.headers.get("authorization") != f"Bearer {self.valid}":
            return httpx.Response(200, json={"data": None, "errors": [{"message": "Operation error", "extensions": {"CAIDO": {"reason": "INVALID_TOKEN", "code": "AUTHORIZATION"}}}]})
        if name == "upstreamProxiesHttp":
            return httpx.Response(200, json={"data": {"upstreamProxiesHttp": self.upstreams}})
        if name == "createUpstreamProxyHttp":
            self.upstreams.append({"id": f"u{len(self.upstreams) + 10}", "enabled": v["i"]["enabled"], "allowlist": v["i"]["allowlist"],
                                   "connection": {"host": v["i"]["connection"]["host"], "port": v["i"]["connection"]["port"]}})
            return httpx.Response(200, json={"data": {"createUpstreamProxyHttp": {"proxy": {"id": self.upstreams[-1]["id"]}}}})
        if name == "deleteUpstreamProxyHttp":
            self.upstreams = [u for u in self.upstreams if u["id"] != v["id"]]
            return httpx.Response(200, json={"data": {"deleteUpstreamProxyHttp": {"deletedId": v["id"]}}})
        if name == "createProject":
            assert v["i"] == {"name": "hunterai", "temporary": True}
            self.projects.append({"id": "p1", "name": "hunterai"})
            return httpx.Response(200, json={"data": {"createProject": {"project": {"id": "p1"}, "error": None}}})
        if name == "selectProject":
            self.current = v["id"]
            return httpx.Response(200, json={"data": {"selectProject": {"currentProject": {"project": {"id": v["id"]}}, "error": None}}})
        if name == "currentProject":
            return httpx.Response(200, json={"data": {"currentProject": {"project": {"id": self.current}} if self.current else None, "projects": self.projects}})
        if name == "runtime":
            return httpx.Response(200, json={"data": {"runtime": {"version": "0.59.0"}, "currentProject": {"project": {"name": "hunterai", "temporary": True}},
                                                      "requestsByOffset": {"count": {"value": 3}}}})
        if name == "requestsByOffset":
            if (v.get("f") or {}).get("code") == "bad (":
                return httpx.Response(200, json={"data": None, "errors": [{"message": "Invalid HTTPQL query"}]})
            self.last_list = v
            return httpx.Response(200, json={"data": {"requestsByOffset": {"count": {"value": 3}, "nodes": [ROW] if not v.get("o") else []}}})
        if name == "request(id":
            found = v["id"] == "7"
            return httpx.Response(200, json={"data": {"request": {**ROW, "raw": b64(REQ), "response": {**ROW["response"], "raw": b64(RESP)}} if found else None}})
        if name == "createReplaySession":
            self.replays.append(v["i"]["requestSource"]["raw"])
            return httpx.Response(200, json={"data": {"createReplaySession": {"session": {"id": "s1"}, "error": None}}})
        if name == "startReplayTask":
            return httpx.Response(200, json={"data": {"startReplayTask": {"task": {"id": "k1", "replayEntry": {"id": "e2"}}, "error": None}}})
        self.polls += 1                                   # replayEntry: answered on the second look
        done = self.polls >= 2
        return httpx.Response(200, json={"data": {"replayEntry": {"error": None, "request": {"id": "9", "response": {"statusCode": 200, "raw": b64(RESP.replace(b"403 Forbidden", b"200 OK"))} if done else None}}}})


@pytest.fixture
async def fake(monkeypatch):
    for name in ("HUNTERAI_CAIDO", "HUNTERAI_CAIDO_URL", "HUNTERAI_CAIDO_PORT", "HUNTERAI_CAIDO_BIN"):
        monkeypatch.delenv(name, raising=False)
    for key in ("token", "egress", "routed"):
        monkeypatch.setitem(caido._state, key, None)
    monkeypatch.setattr(caido, "binary", lambda: "/usr/bin/caido-cli")
    async def none(_):
        return None
    monkeypatch.setattr(caido, "_sleep", none)
    for t in caido_tools.tools():
        registry.register(t)
    f = FakeCaido()
    with respx.mock:
        respx.get(URL + "/").mock(side_effect=f.root)
        respx.post(URL + "/graphql").mock(side_effect=f.graphql)
        yield f
        await caido.shutdown()


async def call(conn, tool, args, policy=None):
    chat, run_id = await seeded_run(conn, mode="pentesting", permissions=policy or {})
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", tool, args))
    return out.block


def test_raw_http_is_shown_as_text_whatever_its_coding():
    assert caido_tools.render(RESP) == 'HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\n\n{"error":"no"}'.replace("\r\n", "\r\n")
    zipped = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Encoding: gzip\r\n\r\n" + gzip.compress(b"<h1>hi</h1>")
    assert caido_tools.render(zipped).endswith("<h1>hi</h1>")
    chunked = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    assert caido_tools.render(chunked).endswith("hello world")
    assert "[binary body, 4 bytes, image/png]" in caido_tools.render(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n\r\n\x89PNG")
    assert "compressed with br, 3 bytes" in caido_tools.render(b"HTTP/1.1 200 OK\r\nContent-Encoding: br\r\n\r\nabc")
    assert "does not decode" in caido_tools.render(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n\r\nnot gzip")
    assert "[body cut at 10 characters of 50]" in caido_tools.render(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n" + b"x" * 50, cap=10)
    assert caido_tools.render(b"GET / HTTP/1.1\r\nHost: a\r\n\r\n") == "GET / HTTP/1.1\r\nHost: a"


def test_a_request_as_a_model_writes_it_becomes_wire_bytes():
    raw, method, target = caido_tools.build('POST /api/login HTTP/2\nContent-Type: application/json\nContent-Length: 999\n\n{"user":"b"}', "app.acme.test", 443, True)
    assert (method, target) == ("POST", "/api/login")
    assert raw == b'POST /api/login HTTP/1.1\r\nHost: app.acme.test\r\nContent-Type: application/json\r\nContent-Length: 12\r\n\r\n{"user":"b"}'
    raw, _, _ = caido_tools.build("GET /x\r\nHost: other.test\r\nX-A: 1\r\n\r\n", "app.acme.test", 8443, True)
    assert raw == b"GET /x HTTP/1.1\r\nHost: other.test\r\nX-A: 1\r\n\r\n"                    # a Host that was given is kept
    assert b"Host: app.acme.test:8443\r\n" in caido_tools.build("GET /", "app.acme.test", 8443, True)[0]
    assert caido_tools.build("POST /x\nTransfer-Encoding: chunked\n\n0\n\n", "a.test", 80, False)[0].count(b"Content-Length") == 0
    for bad in ("", "just words here", "GET /x HTTP/1.1\nnot a header\n\n"):
        with pytest.raises(ToolError):
            caido_tools.build(bad, "a.test", 80, False)


async def test_caido_is_started_signed_into_and_given_a_project_once(fake, monkeypatch):
    fake.up = False
    started = []
    monkeypatch.setattr(caido, "_spawn", lambda: (started.append(1), setattr(fake, "up", True)))
    rows, total = await caido.history(None, 10, 0)
    assert started == [1] and total == 3 and rows[0]["id"] == "7"
    assert fake.calls == ["loginAsGuest", "currentProject", "createProject", "selectProject", "upstreamProxiesHttp", "createUpstreamProxyHttp",
                          "requestsByOffset"]
    await caido.history('req.host.cont:"acme"', 500, 0)
    assert fake.calls[7:] == ["upstreamProxiesHttp", "requestsByOffset"] and fake.last_list == {"l": caido.MAX_ROWS, "o": 0, "f": {"code": 'req.host.cont:"acme"'}}
    assert started == [1]


async def test_a_restarted_caido_is_signed_into_again_and_other_errors_are_told(fake):
    await caido.history(None, 1, 0)
    fake.valid = "someone-else"                          # Caido was restarted: the token it gave is gone
    fake.projects, fake.current = [{"id": "p9", "name": "hunterai"}], None
    await caido.history(None, 1, 0)
    assert fake.calls[-7:] == ["requestsByOffset", "upstreamProxiesHttp", "loginAsGuest", "currentProject", "selectProject", "upstreamProxiesHttp",
                               "requestsByOffset"]
    assert fake.current == "p9"
    before = len(fake.calls)
    with pytest.raises(caido.CaidoError, match="Invalid HTTPQL query"):
        await caido.history("bad (", 1, 0)
    assert fake.calls[before:] == ["upstreamProxiesHttp", "requestsByOffset"]   # not a sign-in problem: asked once


async def test_a_caido_that_is_missing_or_shut_says_so(fake, monkeypatch):
    fake.up = False
    monkeypatch.setattr(caido, "binary", lambda: None)
    with pytest.raises(caido.CaidoError, match="not installed"):
        await caido.history(None, 1, 0)
    assert not caido.available()
    monkeypatch.setenv("HUNTERAI_CAIDO_URL", URL)
    assert caido.available()
    with pytest.raises(caido.CaidoError, match="does not answer at http://127.0.0.1:8791"):
        await caido.history(None, 1, 0)


async def test_the_history_is_listed_and_one_exchange_read(conn, fake):
    said = (await call(conn, "caido", {"action": "requests", "filter": 'resp.code.eq:403'})).content
    assert "3 recorded request(s) match; rows 1-1" in said
    assert "#7 POST https://app.acme.test/api/login?next=1 -> 403, 120 bytes, 35 ms [intercept]" in said
    sent = {**ROW, "id": "9", "host": "93.184.216.34", "source": "REPLAY", "raw": REQ}
    assert caido_tools._row(sent).startswith("#9 POST https://app.acme.test/api/login?next=1 (sent to 93.184.216.34) -> 403")
    assert "Caido's own copy" in caido_tools._row({**sent, "response": None}) and "[replay]" in caido_tools._row(sent)
    assert "No more rows (3 match)" in (await call(conn, "caido", {"action": "requests", "offset": 50})).content
    one = (await call(conn, "caido", {"action": "request", "id": "#7"})).content
    assert "---- request ----\nPOST /api/login?next=1 HTTP/1.1" in one and '{"user":"a"}' in one and "---- response ----\nHTTP/1.1 403 Forbidden" in one
    only = (await call(conn, "caido", {"action": "request", "id": "7", "part": "response"})).content
    assert "---- request ----" not in only and '{"error":"no"}' in only
    for args, why in (({"action": "request", "id": "8"}, "no recorded request #8"), ({"action": "request", "id": "x"}, "give the id"),
                      ({"action": "requests", "filter": "bad ("}, "Invalid HTTPQL query")):
        block = await call(conn, "caido", args)
        assert block.is_error and why in block.content
    from hunterai.repo import settings
    await settings.put(conn, "browser_via_caido", False)            # recording switched off: a command is then told the address itself
    status = (await call(conn, "caido", {"action": "status"})).content
    assert "Caido 0.59.0 is running at http://127.0.0.1:8791" in status and "temporary" in status and "curl -x http://127.0.0.1:8791 -k" in status


async def test_a_request_is_sent_through_caido_to_the_address_that_was_checked(conn, fake, dns):
    table, lookups = dns; table["app.acme.test"] = ["93.184.216.34"]
    block = await call(conn, "caido_send", {"host": "app.acme.test", "raw": 'POST /api/login HTTP/1.1\nContent-Type: application/json\n\n{"user":"b"}'}, policy=NET)
    assert not block.is_error and "Recorded in Caido as request #9." in block.content and "HTTP/1.1 200 OK" in block.content
    sent = fake.replays[0]
    assert sent["connectionInfo"] == {"host": "app.acme.test", "port": 443, "isTLS": True}      # by name: Caido's way out checks and connects
    assert base64.b64decode(sent["raw"]).startswith(b"POST /api/login HTTP/1.1\r\nHost: app.acme.test\r\n") and lookups == ["app.acme.test"]
    plain = await call(conn, "caido_send", {"host": "app.acme.test", "port": 8080, "tls": False, "raw": "GET /health"}, policy=NET)
    assert not plain.is_error and fake.replays[1]["connectionInfo"] == {"host": "app.acme.test", "port": 8080, "isTLS": False}
    bad = await call(conn, "caido_send", {"host": "https://app.acme.test/x", "raw": "GET /"}, policy=NET)
    assert bad.is_error and "without a scheme" in bad.content and len(fake.replays) == 2


async def test_hunterais_filter_is_caidos_only_way_out_and_stale_ones_are_dropped(fake):
    fake.upstreams = [{"id": "u1", "enabled": True, "allowlist": ["*"], "connection": {"host": "127.0.0.1", "port": 1}},        # an earlier run's
                      {"id": "u2", "enabled": True, "allowlist": ["*.corp.test"], "connection": {"host": "10.0.0.9", "port": 3128}}]  # the owner's own
    await caido.history(None, 1, 0)
    out = (await caido.egress()).port
    assert [(u["id"], u["connection"]["port"]) for u in fake.upstreams] == [("u2", 3128), ("u11", out)]
    assert fake.upstreams[1] == {"id": "u11", "enabled": True, "allowlist": ["*"], "connection": {"host": "127.0.0.1", "port": out}}
    before = len(fake.calls)
    await caido.history(None, 1, 0)
    assert fake.calls[before:] == ["upstreamProxiesHttp", "requestsByOffset"]      # in place: looked at, not made again
    fake.upstreams = [u for u in fake.upstreams if u["id"] == "u2"]         # someone removed it in Caido
    await caido.history(None, 1, 0)
    assert fake.upstreams[-1]["connection"]["port"] == out and len(fake.upstreams) == 2
    await caido.shutdown()                                                 # HunterAI restarted: a new filter on a new port
    await caido.history(None, 1, 0)
    assert [u["connection"]["port"] for u in fake.upstreams] == [3128, (await caido.egress()).port] and len(fake.upstreams) == 2
    assert await caido.proxy_address() == ("127.0.0.1", 8791)


async def test_the_way_out_lets_a_local_address_through_only_while_it_is_allowed(fake, monkeypatch):
    from hunterai import browser
    gate = (await caido.egress()).local_ok
    assert ("127.0.0.1", 3000) not in gate
    session = type("S", (), {"local_ok": {("127.0.0.1", 3000)}})()
    monkeypatch.setattr(browser, "_sessions", {"chat": session})
    assert ("127.0.0.1", 3000) in gate and ("127.0.0.1", 6333) not in gate
    with pytest.raises(ToolError, match="this machine or local network"):
        await (await caido.egress()).check("127.0.0.1", 6333)
    assert await (await caido.egress()).check("127.0.0.1", 3000) == "127.0.0.1"


async def test_at_start_a_running_caido_is_rerouted_and_a_stopped_one_is_left_alone(fake, monkeypatch):
    assert await caido.refresh() is True and len(fake.upstreams) == 1 and "createProject" in fake.calls
    fake.up = False
    started = []
    monkeypatch.setattr(caido, "_spawn", lambda: started.append(1))
    assert await caido.refresh() is False and started == []
    monkeypatch.setenv("HUNTERAI_CAIDO", "off")
    fake.up = True
    before = len(fake.calls)
    assert await caido.refresh() is False and not caido.available() and len(fake.calls) == before
