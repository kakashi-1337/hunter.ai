"""The owner's own proxy for the agent's web traffic: Burp, mitmproxy, a tunnel."""
import httpx
import pytest
import respx

from hunterai import browser, upstream
from hunterai.repo import settings
from hunterai.tools import registry, web
from tests.test_api import client  # noqa: F401
from tests.test_tools_web import IP, NET, download, fetch, text


@pytest.fixture(autouse=True)
def web_registered():
    for t in web.tools():
        registry.register(t)


@pytest.mark.parametrize("typed,address", [
    ("", None), ("   ", None),
    ("http://127.0.0.1:8080", ("127.0.0.1", 8080)), ("127.0.0.1:8080", ("127.0.0.1", 8080)),
    ("  HTTP://LocalHost:8080/ ", ("localhost", 8080)), ("burp.lan:8081", ("burp.lan", 8081)),
    ("[::1]:9090", ("::1", 9090)), ("http://[::1]:9090", ("::1", 9090)),
])
def test_an_address_is_read(typed, address):
    assert upstream.parse(typed) == address


@pytest.mark.parametrize("typed,why", [
    ("https://127.0.0.1:8080", "HTTP proxy"), ("socks5://127.0.0.1:9050", "SOCKS"), ("ftp://127.0.0.1:21", "HTTP proxy"),
    ("http://user:secret@127.0.0.1:8080", "password"), ("http://127.0.0.1", "port"), ("127.0.0.1", "port"),
    ("127.0.0.1:0", "port"), ("127.0.0.1:99999", "port"), ("127.0.0.1:abc", "port"),
    ("http://127.0.0.1:8080/path", "host and its port"), ("http://127.0.0.1:8080?x=1", "host and its port"),
    ("http://bad host:8080", "host"), ("http://:8080", "host"),
])
def test_an_address_that_is_refused_says_why(typed, why):
    with pytest.raises(upstream.ProxyError, match=why):
        upstream.parse(typed)


async def test_the_setting_is_kept_and_shown(client):
    s = (await client.get("/api/settings")).json()
    assert (s["upstream_proxy"], s["upstream_proxy_intercepts"]) == ("", False)
    s = (await client.put("/api/settings", json={"upstream_proxy": " 127.0.0.1:8080 "})).json()
    assert (s["upstream_proxy"], s["upstream_proxy_intercepts"]) == ("http://127.0.0.1:8080", False)
    s = (await client.put("/api/settings", json={"upstream_proxy_intercepts": True})).json()
    assert s["upstream_proxy_intercepts"] is True
    bad = await client.put("/api/settings", json={"upstream_proxy": "socks5://127.0.0.1:9050"})
    assert bad.status_code == 400 and "SOCKS" in bad.text
    assert (await client.get("/api/settings")).json()["upstream_proxy"] == "http://127.0.0.1:8080"      # a refused value changes nothing
    s = (await client.put("/api/settings", json={"upstream_proxy": ""})).json()
    assert (s["upstream_proxy"], s["upstream_proxy_intercepts"]) == ("", False)                          # cleared, and its switch with it


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
               upstream.Upstream("proxy.example.net", 3128, True), upstream.Upstream("::1", 9090, True)):
        await web._proxied(up).aclose()
    assert [(k["proxy"], k["verify"], k["trust_env"]) for k in seen] == [
        ("http://127.0.0.1:8080", True, False), ("http://127.0.0.1:8080", False, False),
        ("http://proxy.example.net:3128", True, False),              # never for a proxy elsewhere, whatever the switch says
        ("http://[::1]:9090", False, False)]


async def test_the_browser_takes_the_same_way_out(conn, monkeypatch):
    assert await browser._way_out() == (None, False, False)
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    assert await browser._way_out() == (("127.0.0.1", 8080), False, False)
    await settings.put(conn, "upstream_proxy_intercepts", True)
    assert await browser._way_out() == (("127.0.0.1", 8080), False, True)                # it accepts the proxy's certificates

    async def caido_proxy():
        return "127.0.0.1", 18791
    monkeypatch.setattr(browser, "_caido_proxy", caido_proxy)
    assert await browser._way_out() == (("127.0.0.1", 18791), True, True)                # Caido, when it is switched on
