"""The owner's own proxy for the agent's web traffic: Burp, mitmproxy, a tunnel."""
import gc
import re

import httpx
import pytest
import respx

from hunterai import browser, upstream
from hunterai.repo import settings
from hunterai.tools import registry, web
from tests.test_api import client  # noqa: F401
from tests.proxies import HttpProxy, Site, Socks5Proxy, running
from tests.test_tools_web import IP, NET, download, fetch, text


@pytest.fixture(autouse=True)
def web_registered():
    for t in web.tools():
        registry.register(t)


def where(u):
    return u and (u.scheme, u.user, u.password, u.host, u.port)


@pytest.mark.parametrize("typed,address", [
    ("", None), ("   ", None),
    ("http://127.0.0.1:8080", ("http", "", "", "127.0.0.1", 8080)), ("127.0.0.1:8080", ("http", "", "", "127.0.0.1", 8080)),
    ("  HTTP://LocalHost:8080/ ", ("http", "", "", "localhost", 8080)), ("burp.lan:8081", ("http", "", "", "burp.lan", 8081)),
    ("[::1]:9090", ("http", "", "", "::1", 9090)), ("http://[::1]:9090", ("http", "", "", "::1", 9090)),
    ("socks5://127.0.0.1:1080", ("socks5", "", "", "127.0.0.1", 1080)), ("SOCKS5H://fr.example.net:1080", ("socks5", "", "", "fr.example.net", 1080)),
    ("http://brd-customer-hl_1-zone-dc-country-fr:s3cret@brd.superproxy.io:33335",
     ("http", "brd-customer-hl_1-zone-dc-country-fr", "s3cret", "brd.superproxy.io", 33335)),
    ("socks5://hunter:p%40ss%3Aword@fr.example.net:1080", ("socks5", "hunter", "p@ss:word", "fr.example.net", 1080)),
    ("hunter:pa:ss@fr.example.net:3128", ("http", "hunter", "pa:ss", "fr.example.net", 3128)),       # the first colon ends the user
    ("hunter:p@ss@fr.example.net:3128", ("http", "hunter", "p@ss", "fr.example.net", 3128)),         # the last @ starts the host
    ("hunter@fr.example.net:3128", ("http", "hunter", "", "fr.example.net", 3128)),
])
def test_an_address_is_read(typed, address):
    assert where(upstream.parse(typed)) == address


@pytest.mark.parametrize("typed,why", [
    ("https://127.0.0.1:8080", "http:// or socks5://"), ("socks4://127.0.0.1:9050", "http:// or socks5://"), ("ftp://127.0.0.1:21", "http:// or socks5://"),
    ("http://127.0.0.1", "port"), ("127.0.0.1", "port"),
    ("127.0.0.1:0", "port"), ("127.0.0.1:99999", "port"), ("127.0.0.1:abc", "port"),
    ("http://127.0.0.1:8080/path", "host and its port"), ("http://127.0.0.1:8080?x=1", "host and its port"),
    ("http://bad host:8080", "host"), ("http://:8080", "host"),
    ("http://:secret@127.0.0.1:8080", "user name"), ("http://us er:x@127.0.0.1:8080", "user name"),
    ("http://user:pass\nword@127.0.0.1:8080", "password"),
])
def test_an_address_that_is_refused_says_why(typed, why):
    with pytest.raises(upstream.ProxyError, match=why):
        upstream.parse(typed)


def test_the_password_is_kept_for_the_request_and_out_of_what_is_shown():
    u = upstream.parse("socks5://hunter:p%40ss%3Aword@fr.example.net:1080")
    assert u.url == "socks5://hunter:p%40ss%3Aword@fr.example.net:1080" and where(upstream.parse(u.url)) == where(u)
    assert u.shown == "socks5://hunter@fr.example.net:1080" and u.address == "fr.example.net:1080" and "p@ss" not in repr(u)
    assert upstream.parse("127.0.0.1:8080").shown == upstream.parse("127.0.0.1:8080").url == "http://127.0.0.1:8080"


async def test_the_setting_is_kept_and_shown(client):
    s = (await client.get("/api/settings")).json()
    assert (s["upstream_proxy"], s["upstream_proxy_intercepts"], s["upstream_proxy_password"]) == ("", False, False)
    s = (await client.put("/api/settings", json={"upstream_proxy": " 127.0.0.1:8080 "})).json()
    assert (s["upstream_proxy"], s["upstream_proxy_intercepts"]) == ("http://127.0.0.1:8080", False)
    s = (await client.put("/api/settings", json={"upstream_proxy_intercepts": True})).json()
    assert s["upstream_proxy_intercepts"] is True
    bad = await client.put("/api/settings", json={"upstream_proxy": "socks4://127.0.0.1:9050"})
    assert bad.status_code == 400 and "socks5://" in bad.text
    assert (await client.get("/api/settings")).json()["upstream_proxy"] == "http://127.0.0.1:8080"      # a refused value changes nothing
    s = (await client.put("/api/settings", json={"upstream_proxy": ""})).json()
    assert (s["upstream_proxy"], s["upstream_proxy_intercepts"]) == ("", False)                          # cleared, and its switch with it


async def test_a_password_is_stored_and_never_shown_again(client, conn):
    s = (await client.put("/api/settings", json={"upstream_proxy": "socks5://hunter:s3cret@fr.example.net:1080"})).json()
    assert (s["upstream_proxy"], s["upstream_proxy_password"]) == ("socks5://hunter@fr.example.net:1080", True) and "s3cret" not in str(s)
    assert (await upstream.get(conn)).password == "s3cret"
    s = (await client.put("/api/settings", json={"upstream_proxy": s["upstream_proxy"], "swarm_width": 2})).json()   # the form sends back what it was shown
    assert s["upstream_proxy_password"] is True and (await upstream.get(conn)).password == "s3cret"
    s = (await client.put("/api/settings", json={"upstream_proxy": "socks5://other@fr.example.net:1080"})).json()     # another user: the old password is not theirs
    assert s["upstream_proxy_password"] is False and (await upstream.get(conn)).password == ""
    await client.put("/api/settings", json={"upstream_proxy": "socks5://other:new@fr.example.net:1080"})
    assert (await upstream.get(conn)).password == "new"


async def test_only_a_proxy_on_this_machine_may_read_https(client):
    await client.put("/api/settings", json={"upstream_proxy": "proxy.example.net:3128"})
    r = await client.put("/api/settings", json={"upstream_proxy_intercepts": True})
    assert r.status_code == 400 and "this machine" in r.text
    await client.put("/api/settings", json={"upstream_proxy": "127.0.0.1:8080", "upstream_proxy_intercepts": True})
    moved = (await client.put("/api/settings", json={"upstream_proxy": "proxy.example.net:3128"})).json()
    assert moved["upstream_proxy_intercepts"] is False                                                   # the switch does not travel to another machine


async def test_caido_and_another_proxy_are_not_combined(client):
    await client.put("/api/settings", json={"browser_via_caido": True})
    r = await client.put("/api/settings", json={"upstream_proxy": "127.0.0.1:8080"})
    assert r.status_code == 400 and "Caido" in r.text
    both = await client.put("/api/settings", json={"browser_via_caido": False, "upstream_proxy": "127.0.0.1:8080"})
    assert both.status_code == 200 and both.json()["upstream_proxy"] == "http://127.0.0.1:8080"
    r = await client.put("/api/settings", json={"browser_via_caido": True})
    assert r.status_code == 400 and "proxy" in r.text


@respx.mock
async def test_a_fetch_goes_through_the_proxy_by_name(conn, dns, monkeypatch):
    dns[0]["example.test"] = [IP]
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    made = []
    real = web._proxied
    monkeypatch.setattr(web, "_proxied", lambda up: (made.append(up), real(up))[1])
    named = respx.get("http://example.test/a").mock(return_value=text("hello"))
    pinned = respx.get(f"http://{IP}/a").mock(return_value=text("direct"))
    out = await fetch(conn, {"url": "http://example.test/a"}, policy=NET)
    assert "hello" in out.block.content and "sent through the proxy at 127.0.0.1:8080" in out.block.content
    assert named.called and not pinned.called and made == [upstream.Upstream("127.0.0.1", 8080, False)]


@respx.mock
async def test_nothing_is_sent_directly_when_the_proxy_does_not_answer(conn, dns):
    dns[0]["example.test"] = [IP]
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    respx.get("http://example.test/a").mock(side_effect=httpx.ProxyError("refused"))
    pinned = respx.get(f"http://{IP}/a").mock(return_value=text("direct"))
    out = await fetch(conn, {"url": "http://example.test/a"}, policy=NET)
    assert "the proxy at 127.0.0.1:8080" in out.block.content and "Nothing was sent directly" in out.block.content
    assert out.block.is_error and not pinned.called


@respx.mock
async def test_a_download_goes_through_the_proxy_too(conn, dns):
    dns[0]["files.test"] = [IP]
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    named = respx.get("http://files.test/a.bin").mock(return_value=httpx.Response(200, content=b"abcd"))
    pinned = respx.get(f"http://{IP}/a.bin").mock(return_value=httpx.Response(200, content=b"zzzz"))
    out, _ = await download(conn, {"url": "http://files.test/a.bin"}, policy={"risk": {"network": "allow", "write": "allow"}})
    assert named.called and not pinned.called and "4 bytes" in out.block.content


async def test_certificates_are_checked_unless_the_owner_runs_a_proxy_that_reads_https(monkeypatch):
    seen = []
    real = httpx.AsyncClient
    monkeypatch.setattr(web.httpx, "AsyncClient", lambda **kw: (seen.append(kw), real(**kw))[1])
    for up in (upstream.Upstream("127.0.0.1", 8080, False), upstream.Upstream("127.0.0.1", 8080, True),
               upstream.Upstream("proxy.example.net", 3128, True), upstream.Upstream("::1", 9090, True),
               upstream.Upstream("proxy.example.net", 1080, scheme="socks5", user="hunter", password="s3cret-pass")):
        await (await web._proxied(up)).aclose()
    assert [(k["verify"], k["trust_env"]) for k in seen] == [
        (True, False), (False, False),
        (True, False),               # never for a proxy elsewhere, whatever the switch says
        (False, False), (True, False)]
    ways = [k["proxy"] for k in seen]                    # the client is pointed at the hop, which holds the login
    assert all(re.fullmatch(r"http://127\.0\.0\.1:\d+", w) for w in ways) and len(set(ways)) == 5


async def test_the_browser_takes_the_same_way_out(conn, monkeypatch):
    from hunterai.repo import chats, targets
    assert await browser._way_out() == (None, False, False)
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    default = upstream.Upstream("127.0.0.1", 8080)
    assert await browser._way_out() == (default, False, False)
    await settings.put(conn, "upstream_proxy_intercepts", True)
    reading = upstream.Upstream("127.0.0.1", 8080, intercepts=True)
    assert await browser._way_out() == (reading, False, True)                            # it accepts the proxy's certificates

    target = await targets.create(conn, "french program", "", {"in": ["*.acme.test"], "out": []})
    chat = await chats.create(conn, target.id, "pentesting", "mock", "mock-1", {})
    plain = await chats.create(conn, None, "pentesting", "mock", "mock-1", {})
    assert (await browser._way_out(chat.id))[0] == reading                                # a target without its own takes the default
    await upstream.put_target(conn, target.id, "socks5://hunter:s3cret@fr.example.net:1080")
    own = upstream.Upstream("fr.example.net", 1080, scheme="socks5", user="hunter", password="s3cret")
    assert await browser._way_out(chat.id) == (own, False, False)                         # its own proxy for its own chats
    assert (await browser._way_out(plain.id))[0] == reading                               # and no other chat's
    assert await browser._way_out(browser.CRAWL) == (None, False, False)                  # the knowledge crawler: only when asked
    await settings.put(conn, "upstream_proxy_kb", True)
    assert (await browser._way_out(browser.CRAWL))[0] == reading

    async def caido_proxy():
        return "127.0.0.1", 18791
    from hunterai import caido
    monkeypatch.setattr(caido, "available", lambda: True)
    monkeypatch.setattr(caido, "proxy_address", caido_proxy)
    await settings.put(conn, "browser_via_caido", True)
    assert await browser._way_out(plain.id) == (upstream.Upstream("127.0.0.1", 18791), True, True)   # Caido, when it is switched on
    from hunterai.tools.base import ToolError
    with pytest.raises(ToolError, match="proxy of its own"):                              # never Caido in place of a proxy a program asked for
        await browser._way_out(chat.id)
    await targets.delete(conn, target.id)
    assert await upstream.of_target(conn, target.id) is None                              # gone with its target


async def test_an_open_browser_session_does_not_outlive_its_way_out(conn, monkeypatch):
    from hunterai.repo import chats, targets
    from hunterai.tools.base import ToolError
    launched = []

    async def launch(chat_id):
        launched.append(chat_id)
        s = browser.Session(chat_id, None, None, object())
        s.way = await browser._way_out(chat_id)              # as the real launch records it
        return s
    monkeypatch.setattr(browser, "_launch", launch)
    monkeypatch.setattr(browser, "_sessions", {})
    target = await targets.create(conn, "french program", "", {"in": [], "out": []})
    chat = await chats.create(conn, target.id, "pentesting", "mock", "mock-1", {})
    first = await browser.get(chat.id, create=True)
    assert await browser.get(chat.id) is first                                              # nothing changed: the same session
    await upstream.put_target(conn, target.id, "socks5://hunter:s3cret@fr.example.net:1080")
    with pytest.raises(ToolError, match="proxy for this chat changed"):                     # a tool that reads the page is told to open it again
        await browser.get(chat.id)
    second = await browser.get(chat.id, create=True)
    assert second is not first and second.way[0].address == "fr.example.net:1080" and launched == [chat.id] * 2
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")                     # the default changes; this target has its own
    assert await browser.get(chat.id) is second
    await chats.update(conn, chat.id, target_id=None)                                       # the chat leaves its target
    third = await browser.get(chat.id, create=True)
    assert third is not second and third.way[0].address == "127.0.0.1:8080" and launched == [chat.id] * 3


UNROUTED = "203.0.113.5"       # the tests below use real sockets: a request sent directly by mistake reaches no one


async def _on_target(conn, url, proxy_text, scope=None):
    """web_fetch in a chat on a new target that has `proxy_text` as its own proxy (None: it has none)."""
    from hunterai.providers.base import ToolCall
    from tests.toolkit import run_call, seeded_run
    chat, run_id = await seeded_run(conn, mode="pentesting", scope=scope or {"in": ["*.acme.test"], "out": []}, permissions=NET)
    if proxy_text is not None:
        await upstream.put_target(conn, chat.target_id, proxy_text)
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", "web_fetch", {"url": url}))
    return out.block


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_fetch_goes_through_the_targets_own_proxy_with_its_login(conn, dns, kind):
    dns[0]["shop.acme.test"] = [UNROUTED]
    site = Site()
    carrier = (HttpProxy if kind == "http" else Socks5Proxy)(site, "hunter", "s3cret")
    elsewhere = HttpProxy(site)                                                           # the default from Settings: not for this target
    async with running(site, carrier, elsewhere):
        await settings.put(conn, "upstream_proxy", f"http://127.0.0.1:{elsewhere.port}")
        got = await _on_target(conn, "http://shop.acme.test/cart", f"{kind}://hunter:s3cret@127.0.0.1:{carrier.port}")
    assert not got.is_error and "site says: shop.acme.test/cart" in got.content, got.content
    assert f"sent through the proxy at 127.0.0.1:{carrier.port}" in got.content and "s3cret" not in got.content
    assert carrier.asked == ([("GET", "http://shop.acme.test/cart")] if kind == "http" else [("shop.acme.test", 80)])
    assert elsewhere.asked == []


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_proxy_that_refuses_the_login_stops_the_fetch_and_nothing_is_sent_directly(conn, dns, kind):
    dns[0]["shop.acme.test"] = [UNROUTED]
    site = Site()
    carrier = (HttpProxy if kind == "http" else Socks5Proxy)(site, "hunter", "s3cret")
    async with running(site, carrier):
        got = await _on_target(conn, "http://shop.acme.test/", f"{kind}://hunter:wrong-one@127.0.0.1:{carrier.port}")
    assert got.is_error and "Nothing was sent directly" in got.content
    assert f"the proxy at 127.0.0.1:{carrier.port} did not carry the request to shop.acme.test: the proxy refused the user name or the password" in got.content
    assert site.requests == [] and "wrong-one" not in got.content
    gc.collect()                       # a connection left open after the refusal is reported here, in this test


async def test_what_a_proxy_service_says_about_its_own_answer_is_shown(conn, dns):
    dns[0]["shop.acme.test"] = [UNROUTED]
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    with respx.mock:
        respx.get("http://shop.acme.test/a").mock(return_value=text("blocked", status=403, **{
            "x-brd-err-code": "policy_20050", "proxy-status": "brd; error=http_request_denied", "server": "nginx"}))
        out = await fetch(conn, {"url": "http://shop.acme.test/a"}, policy=NET)
    assert "HTTP 403" in out.block.content and "x-brd-err-code: policy_20050" in out.block.content
    assert "proxy-status: brd; error=http_request_denied" in out.block.content and "server: nginx" not in out.block.content


async def test_a_target_with_its_own_proxy_is_not_carried_by_caido_instead(conn, dns, monkeypatch):
    dns[0]["shop.acme.test"] = [UNROUTED]

    async def caido_is_on(ctx):
        return ("127.0.0.1", 18791), ""
    monkeypatch.setattr(web, "_via_caido", caido_is_on)
    site = Site()
    carrier = HttpProxy(site)
    async with running(site, carrier):
        got = await _on_target(conn, "http://shop.acme.test/", f"http://127.0.0.1:{carrier.port}")
    assert got.is_error and "proxy of its own" in got.content and "Nothing was sent" in got.content
    assert site.requests == [] and carrier.asked == []


async def test_a_fetch_that_caido_could_not_record_says_so_when_it_went_through_the_proxy(conn, dns, monkeypatch):
    dns[0]["shop.acme.test"] = [UNROUTED]

    async def caido_is_down(ctx):
        return None, "not recorded in Caido: it does not answer"
    monkeypatch.setattr(web, "_via_caido", caido_is_down)
    site = Site()
    carrier = HttpProxy(site)
    async with running(site, carrier):
        got = await _on_target(conn, "http://shop.acme.test/", f"http://127.0.0.1:{carrier.port}")
    assert not got.is_error and f"sent through the proxy at 127.0.0.1:{carrier.port}" in got.content
    assert "not recorded in Caido: it does not answer" in got.content


async def test_a_proxy_login_is_kept_out_of_what_tools_return(conn):
    import base64

    from hunterai import secrets
    from hunterai.repo import targets
    target = await targets.create(conn, "french program", "", {"in": [], "out": []})
    await upstream.put_target(conn, target.id, "socks5://hunter:p%40ss-w0rd!@fr.example.net:1080")
    await settings.put(conn, "upstream_proxy", "http://me:password@127.0.0.1:8080")
    header = base64.b64encode(b"me:password").decode()
    printed = ("stored=socks5://hunter:p%40ss-w0rd%21@fr.example.net:1080 typed=hunter:p@ss-w0rd!@fr.example.net "
               f"default=http://me:password@127.0.0.1:8080 Proxy-Authorization: Basic {header}")
    clean = ("stored=socks5://hunter:[redacted]@fr.example.net:1080 typed=hunter:[redacted]@fr.example.net "
             "default=http://me:[redacted]@127.0.0.1:8080 Proxy-Authorization: Basic [redacted]")
    upstream._logins.clear()                             # a new start of the app: nothing has been read yet
    assert secrets.scrub(printed) == printed
    await upstream.remember(conn)                        # what the app does when it starts
    assert secrets.scrub(printed) == clean
    # Only the login is masked, where it stands as one: a password that is an everyday word stays readable elsewhere,
    form = '<input name="password"> the password field; curl -d "user=me&password=x"'
    assert secrets.scrub(form) == form
    assert "password" not in secrets.values()            # and a call that names the word is not refused for it


async def _ask(reader, writer, host, path="/x"):
    writer.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    answer = (await reader.read()).decode()
    writer.close()
    return answer


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_tunnel_is_opened_through_a_proxy_that_wants_a_password(kind):
    site = Site()
    proxy = (HttpProxy if kind == "http" else Socks5Proxy)(site, "hunter", "s3:cret@1")
    async with running(site, proxy):
        up = upstream.Upstream("127.0.0.1", proxy.port, scheme=kind, user="hunter", password="s3:cret@1")
        reader, writer = await upstream.open_tunnel(up, "shop.acme.test", 80, 5)
        assert "site says: shop.acme.test/x" in await _ask(reader, writer, "shop.acme.test")
    assert proxy.asked == ([("CONNECT", "shop.acme.test:80")] if kind == "http" else [("shop.acme.test", 80)])   # asked by name: the proxy looks it up


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_proxy_that_refuses_the_login_says_so_and_nothing_reaches_the_site(kind):
    site = Site()
    proxy = (HttpProxy if kind == "http" else Socks5Proxy)(site, "hunter", "s3cret")
    async with running(site, proxy):
        for login in (("hunter", "wrong"), ("", "")):
            up = upstream.Upstream("127.0.0.1", proxy.port, scheme=kind, user=login[0], password=login[1])
            with pytest.raises(upstream.ProxyFailed, match="user name"):
                await upstream.open_tunnel(up, "shop.acme.test", 80, 5)
    assert site.requests == [] and proxy.asked == []


async def test_a_socks_proxy_with_no_login_is_asked_for_an_address_as_an_address():
    site = Site()
    proxy = Socks5Proxy(site)
    async with running(site, proxy):
        reader, writer = await upstream.open_tunnel(upstream.Upstream("127.0.0.1", proxy.port, scheme="socks5"), "203.0.113.9", 8080, 5)
        assert "site says: 203.0.113.9:8080/x" in await _ask(reader, writer, "203.0.113.9:8080")
    assert proxy.asked == [("203.0.113.9", 8080)] and proxy.logins == []


async def test_a_proxy_that_is_not_there_is_an_error():
    site = Site()
    async with running(site):
        gone = site.port                                  # a port that was just in use and is free again after the block
    with pytest.raises(OSError):
        await upstream.open_tunnel(upstream.Upstream("127.0.0.1", gone, scheme="socks5"), "shop.acme.test", 80, 2)
