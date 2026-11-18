"""Recording in Caido is on from the start where there is a Caido and no proxy of the owner's, Caido is started
with HunterAI, and a site's certificate is checked by HunterAI itself before a request is handed to Caido: Caido
passes on a certificate no authority signed, and one that has expired, without a word."""
import asyncio
import datetime
import ssl

import httpx
import pytest
import respx

from hunterai import browser, browser_proxy, caido, certs, upstream
from hunterai.providers.base import ToolCall
from hunterai.repo import settings
from hunterai.tools import registry, shell, web
from hunterai.tools.base import ToolError
from tests.proxies import HttpProxy, Site, running
from tests.test_api import client  # noqa: F401 - a fixture: the API of a HunterAI with its own home
from tests.test_tools_caido import URL, fake  # noqa: F401 - `fake` is a fixture: a Caido that answers by what is asked
from tests.test_tools_web import IP, NET
from tests.toolkit import run_call, seeded_run


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    for t in (*web.tools(), *shell.tools()):          # the caido tools come with the `fake` fixture
        registry.register(t)
    monkeypatch.delenv("CAIDO_PROXY", raising=False)
    monkeypatch.setitem(caido._unreached, "until", 0.0)      # noqa: SLF001


def a_caido(monkeypatch, port: int | None = 18791) -> list:
    """There is a Caido on this machine; its proxy is at `port`, or cannot be reached. Returns the times it was asked."""
    asked = []

    async def address():
        asked.append(1)
        if port is None:
            raise caido.CaidoError("Caido does not answer at http://127.0.0.1:8791")
        return "127.0.0.1", port
    monkeypatch.setattr(caido, "available", lambda: True)
    monkeypatch.setattr(caido, "proxy_address", address)
    return asked


async def called(conn, chat, run_id, tool: str, args: dict):
    return await asyncio.wait_for(run_call(conn, chat, run_id, ToolCall("c", tool, args)), 30)


# ---- on from the start

async def test_recording_is_on_where_the_owner_never_said_and_nothing_stands_against_it(conn, monkeypatch):
    monkeypatch.setattr(caido, "available", lambda: False)
    assert await caido.mode(conn) == "off" and not await caido.recording(conn)                 # no Caido on this machine
    a_caido(monkeypatch)
    assert await caido.mode(conn) == "default" and await caido.recording(conn) and await caido.recording()
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    assert await caido.mode(conn) == "off"                                                     # a proxy of the owner's carries the traffic
    await settings.put(conn, "upstream_proxy", "")
    await settings.put(conn, "browser_via_caido", False)
    assert await caido.mode(conn) == "off" and not await caido.recording(conn)                 # the owner switched it off
    await settings.put(conn, "browser_via_caido", True)
    assert await caido.mode(conn) == "on"
    await settings.put(conn, "upstream_proxy", "http://127.0.0.1:8080")
    assert await caido.mode(conn) == "on"                                                      # what the owner chose stays chosen


async def test_a_command_gets_the_address_without_anyone_having_switched_recording_on(conn, monkeypatch):
    a_caido(monkeypatch)
    chat, run_id = await seeded_run(conn, permissions={"risk": {"execute": "allow"}})
    out, _, _ = await called(conn, chat, run_id, "execute", {"command": 'echo "at $CAIDO_PROXY"'})
    assert "at http://127.0.0.1:18791" in out.block.content.splitlines()


async def test_a_fetch_goes_through_caido_by_default_and_a_target_with_its_own_proxy_keeps_that(conn, monkeypatch):
    asked = a_caido(monkeypatch)
    chat, run_id = await seeded_run(conn, scope={"in": ["*.acme.test"], "out": []})
    ctx = type("Ctx", (), {"target": type("T", (), {"id": chat.target_id})()})()
    assert await web._via_caido(ctx) == (("127.0.0.1", 18791), "") and await web._via_caido(None) == (("127.0.0.1", 18791), "")   # noqa: SLF001
    await upstream.put_target(conn, chat.target_id, "http://127.0.0.1:8080")
    before = len(asked)
    assert await web._via_caido(ctx) == (None, "") and len(asked) == before       # noqa: SLF001 - not recorded, not stopped, Caido not asked
    assert (await web._own_proxy(ctx)).address == "127.0.0.1:8080"                # noqa: SLF001
    await settings.put(conn, "browser_via_caido", True)                           # the owner asked for both: that is said, as before
    assert await web._via_caido(ctx) == (("127.0.0.1", 18791), "")                # noqa: SLF001
    with pytest.raises(ToolError, match="The two are not combined"):
        await web._own_proxy(ctx, caido_carries=True)                             # noqa: SLF001


async def test_a_browser_session_by_default_goes_through_caido_or_without_it_never_nowhere(conn, monkeypatch):
    a_caido(monkeypatch)
    chat, _ = await seeded_run(conn, scope={"in": ["*.acme.test"], "out": []})
    via, recorded, own_certificates = await browser._way_out(chat.id)             # noqa: SLF001
    assert (via.address, recorded, own_certificates) == ("127.0.0.1:18791", True, True)
    assert await browser._way_out(browser.CRAWL) == (None, False, False)          # noqa: SLF001 - a crawl is not work on a target
    await upstream.put_target(conn, chat.target_id, "http://127.0.0.1:8080")       # the target's own proxy: that one, and no error
    via, recorded, _ = await browser._way_out(chat.id)                            # noqa: SLF001
    assert (via.address, recorded) == ("127.0.0.1:8080", False)
    await upstream.put_target(conn, chat.target_id, None)
    a_caido(monkeypatch, None)                                                     # a Caido that does not come up does not stop browsing
    assert await browser._way_out(chat.id) == (None, False, False)                # noqa: SLF001
    await settings.put(conn, "browser_via_caido", True)                           # ... unless the owner asked for it by name
    with pytest.raises(ToolError, match="Caido is not usable"):
        await browser._way_out(chat.id)                                           # noqa: SLF001


async def test_settings_show_what_is_in_effect_and_a_proxy_can_be_set_over_the_default(client, monkeypatch):
    assert (await client.get("/api/settings")).json()["browser_via_caido"] is False              # no Caido here
    a_caido(monkeypatch)
    assert (await client.get("/api/settings")).json()["browser_via_caido"] is True               # on, and nobody switched it
    r = await client.put("/api/settings", json={"upstream_proxy": "127.0.0.1:8080"})             # not refused: nobody chose Caido
    assert r.status_code == 200, r.text
    assert r.json()["browser_via_caido"] is False and r.json()["upstream_proxy"] == "http://127.0.0.1:8080"
    await client.put("/api/settings", json={"upstream_proxy": ""})
    assert (await client.get("/api/settings")).json()["browser_via_caido"] is True
    r = await client.put("/api/settings", json={"browser_via_caido": False})                     # switched off stays off
    assert r.json()["browser_via_caido"] is False and (await client.get("/api/settings")).json()["browser_via_caido"] is False
    await client.put("/api/settings", json={"browser_via_caido": True})
    r = await client.put("/api/settings", json={"upstream_proxy": "127.0.0.1:8080"})             # chosen by name: the two are not combined
    assert r.status_code == 400 and "already go through Caido" in r.json()["detail"]


# ---- started with HunterAI

async def test_caido_is_started_with_hunterai_while_recording_is_on(conn, fake, monkeypatch):
    started = []
    monkeypatch.setattr(caido, "_spawn", lambda: (started.append(1), setattr(fake, "up", True)))
    fake.up = False
    assert await caido.at_start() is True and started == [1] and len(fake.upstreams) == 1         # running, with HunterAI's filter as its way out


async def test_caido_is_not_started_for_an_owner_who_switched_recording_off(conn, fake, monkeypatch):
    started = []
    monkeypatch.setattr(caido, "_spawn", lambda: (started.append(1), setattr(fake, "up", True)))
    await settings.put(conn, "browser_via_caido", False)
    fake.up = False
    assert await caido.at_start() is False and started == []
    fake.up = True                                             # one that runs anyway still gets this run's filter, as before
    assert await caido.at_start() is True and len(fake.upstreams) == 1 and started == []


async def test_a_caido_that_does_not_come_up_does_not_stop_hunterai_from_starting(conn, monkeypatch):
    a_caido(monkeypatch, None)
    assert await caido.at_start() is False


async def test_the_app_starts_caido_when_it_starts(monkeypatch, isolated_home):
    from hunterai.api.app import create_app
    seen = []

    async def at_start():
        seen.append(1)
        return True
    monkeypatch.setattr(caido, "at_start", at_start)
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    app = create_app()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        assert seen == [1]


# ---- the certificate of a site, checked by HunterAI itself

def certificates(tmp_path):
    """Files of a small authority and of four sites it does or does not vouch for; (name -> (cert, key)), the authority's PEM."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    day = datetime.timedelta(days=1)
    now = datetime.datetime.now(datetime.UTC)

    def made(common, issuer=None, issuer_key=None, ca=False, names=(), start=now - day, end=now + 30 * day):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])
        b = (x509.CertificateBuilder().subject_name(name).issuer_name(issuer.subject if issuer else name).public_key(key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if names:
            b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        return b.sign(issuer_key or key, hashes.SHA256()), key

    authority, authority_key = made("Test Authority", ca=True)
    out = {}
    for label, how in (("good", {"issuer": authority, "issuer_key": authority_key, "names": ["shop.acme.test"]}),
                       ("self-signed", {"names": ["shop.acme.test"]}),
                       ("expired", {"issuer": authority, "issuer_key": authority_key, "names": ["shop.acme.test"], "start": now - 30 * day, "end": now - day}),
                       ("wrong-name", {"issuer": authority, "issuer_key": authority_key, "names": ["other.acme.test"]})):
        cert, key = made("shop.acme.test", **how)
        c, k = tmp_path / f"{label}.crt", tmp_path / f"{label}.key"
        c.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        out[label] = (str(c), str(k))
    return out, authority.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
async def sites(tmp_path, monkeypatch):
    """serve(label) -> the port of an https site with that certificate; `connections` counts what reached them.
    The check here is the real one, and it trusts the small authority of this test and nothing else."""
    pytest.importorskip("cryptography")
    files, authority = certificates(tmp_path)
    monkeypatch.setattr(certs, "_handshake", certs.HANDSHAKE)                     # the real one: conftest puts a stand-in there
    monkeypatch.setattr(certs, "_context", lambda: ssl.create_default_context(cadata=authority))
    servers, connections = [], []

    async def serve(label: str | None) -> int:
        async def answer(reader, writer):
            connections.append(label)
            writer.close()
        ctx = None
        if label is not None:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*files[label])
        s = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=ctx)
        servers.append(s)
        return s.sockets[0].getsockname()[1]
    yield type("S", (), {"serve": staticmethod(serve), "connections": connections})
    for s in servers:
        s.close()


async def test_a_certificate_that_is_good_passes_and_is_not_asked_for_again_for_a_while(sites, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(certs, "_now", lambda: clock[0])
    port = await sites.serve("good")
    await certs.check("shop.acme.test", "127.0.0.1", port)
    await asyncio.sleep(0.05)
    assert len(sites.connections) == 1
    clock[0] += certs.KEEP - 1
    await certs.check("shop.acme.test", "127.0.0.1", port)
    await certs.check("SHOP.acme.test", "127.0.0.1", port)
    await asyncio.sleep(0.05)
    assert len(sites.connections) == 1                         # found good a moment ago
    clock[0] += 2
    await certs.check("shop.acme.test", "127.0.0.1", port)
    await asyncio.sleep(0.05)
    assert len(sites.connections) == 2


@pytest.mark.parametrize("label,why", [("self-signed", "self-signed certificate"), ("expired", "certificate has expired"),
                                       ("wrong-name", "certificate is not valid for 'shop.acme.test'")])
async def test_a_certificate_that_is_not_good_stops_the_call_each_time(sites, label, why):
    port = await sites.serve(label)
    for _ in range(2):                                         # a bad one is never remembered as good
        with pytest.raises(ToolError) as e:
            await certs.check("shop.acme.test", "127.0.0.1", port)
        assert str(e.value).startswith("the certificate of shop.acme.test is not valid (") and why in str(e.value)
        assert str(e.value).endswith("Nothing was sent. While it records in Caido, HunterAI checks a site's certificate itself: "
                                     "Caido passes one like this on. To work with this site all the same, use curl -k in a command.")


async def test_a_site_whose_certificate_cannot_be_seen_stops_the_call_too(sites):
    plain = await sites.serve(None)                            # it answers, and not with TLS
    with pytest.raises(ToolError, match=r"^the certificate of shop\.acme\.test could not be checked \("):
        await certs.check("shop.acme.test", "127.0.0.1", plain)
    s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    closed = s.sockets[0].getsockname()[1]
    s.close()
    await s.wait_closed()
    with pytest.raises(ToolError, match="could not be checked .*nothing answers there.*Nothing was sent"):
        await certs.check("shop.acme.test", "127.0.0.1", closed)


def checks(monkeypatch, bad: str = "") -> list:
    """The certificate checks that are made from here on; with `bad`, each of them fails with that reason."""
    seen = []

    async def check(host, ip, port):
        seen.append((host, ip, port))
        if bad:
            raise ToolError(f"the certificate of {host} is not valid ({bad}). Nothing was sent.")
    monkeypatch.setattr(certs, "check", check)
    return seen


def recording(monkeypatch, on=True):
    async def via(ctx):
        return (("127.0.0.1", 18791), "") if on else (None, "")
    monkeypatch.setattr(web, "_via_caido", via)


async def fetch(conn, tool: str, url: str):
    chat, run_id = await seeded_run(conn, permissions=NET)
    out, _, _ = await called(conn, chat, run_id, tool, {"url": url})
    return out.block


@respx.mock
async def test_a_fetch_and_a_download_through_caido_check_the_sites_certificate_first(conn, dns, monkeypatch):
    dns[0]["shop.test"] = [IP]
    recording(monkeypatch)
    seen = checks(monkeypatch)
    page = respx.get("https://shop.test/a").mock(return_value=httpx.Response(200, text="hello", headers={"content-type": "text/plain"}))
    respx.get("https://shop.test:8443/b.json").mock(return_value=httpx.Response(200, content=b"{}", headers={"content-type": "application/json"}))
    block = await fetch(conn, "web_fetch", "https://shop.test/a")
    assert "hello" in block.content and "recorded in Caido" in block.content and seen == [("shop.test", IP, 443)] and page.call_count == 1
    block = await fetch(conn, "web_download", "https://shop.test:8443/b.json")
    assert block.content.endswith("; recorded in Caido") and seen[1:] == [("shop.test", IP, 8443)]


@respx.mock
async def test_a_bad_certificate_stops_a_fetch_and_a_download_before_anything_is_sent(conn, dns, monkeypatch):
    dns[0]["shop.test"] = [IP]
    recording(monkeypatch)
    checks(monkeypatch, bad="self-signed certificate")
    named = respx.get("https://shop.test/a").mock(return_value=httpx.Response(200, text="hello"))
    for tool in ("web_fetch", "web_download"):
        block = await fetch(conn, tool, "https://shop.test/a")
        assert block.is_error and "the certificate of shop.test is not valid (self-signed certificate). Nothing was sent." in block.content
    assert not named.called


@respx.mock
async def test_the_check_is_for_https_through_caido_and_nothing_else(conn, dns, monkeypatch):
    dns[0]["shop.test"] = [IP]
    seen = checks(monkeypatch, bad="it must not be asked")
    recording(monkeypatch)
    respx.get("http://shop.test/a").mock(return_value=httpx.Response(200, text="plain", headers={"content-type": "text/plain"}))
    assert "plain" in (await fetch(conn, "web_fetch", "http://shop.test/a")).content and seen == []      # no certificate to check
    recording(monkeypatch, on=False)
    respx.get(f"https://{IP}/a").mock(return_value=httpx.Response(200, text="direct", headers={"content-type": "text/plain"}))
    assert "direct" in (await fetch(conn, "web_fetch", "https://shop.test/a")).content and seen == []    # the fetch itself checks it there


@respx.mock
async def test_a_redirect_from_http_to_https_is_checked_when_it_gets_there(conn, dns, monkeypatch):
    dns[0]["shop.test"] = [IP]
    recording(monkeypatch)
    seen = checks(monkeypatch)
    respx.get("http://shop.test/a").mock(return_value=httpx.Response(301, headers={"location": "https://shop.test/a"}))
    respx.get("https://shop.test/a").mock(return_value=httpx.Response(200, text="secure", headers={"content-type": "text/plain"}))
    assert "secure" in (await fetch(conn, "web_fetch", "http://shop.test/a")).content and seen == [("shop.test", IP, 443)]


async def test_caido_send_checks_the_certificate_before_caido_is_asked_to_send(conn, dns, fake, monkeypatch):
    dns[0]["app.acme.test"] = [IP]
    seen = checks(monkeypatch)
    chat, run_id = await seeded_run(conn, permissions=NET)
    out, _, _ = await called(conn, chat, run_id, "caido_send", {"host": "app.acme.test", "raw": "GET /health"})
    assert not out.block.is_error and seen == [("app.acme.test", IP, 443)] and len(fake.replays) == 1
    out, _, _ = await called(conn, chat, run_id, "caido_send", {"host": "app.acme.test", "port": 8080, "tls": False, "raw": "GET /health"})
    assert not out.block.is_error and len(seen) == 1 and len(fake.replays) == 2                     # no TLS: nothing to check
    checks(monkeypatch, bad="certificate has expired")
    out, _, _ = await called(conn, chat, run_id, "caido_send", {"host": "app.acme.test", "raw": "GET /health"})
    assert out.block.is_error and "is not valid (certificate has expired)" in out.block.content and len(fake.replays) == 2


# ---- the browser: its proxy checks before a TLS connection is handed to Caido

async def through_the_browsers_proxy(dns, monkeypatch, first: bytes, bad: str = "", verify: bool = True):
    """What a browser gets when it opens a tunnel to shop.acme.test and sends `first`; (reply, what Caido was asked,
    the checks made, what was blocked)."""
    site = Site()
    stand_in = HttpProxy(site)                                  # in Caido's place
    seen = checks(monkeypatch, bad=bad)
    blocked = []
    async with running(site, stand_in):
        dns[0]["shop.acme.test"] = ["127.0.0.1"]
        p = browser_proxy.Proxy(on_block=blocked.append)
        p.via, p.verify_sites = upstream.Upstream("127.0.0.1", stand_in.port), verify
        p.local_ok.add(("shop.acme.test", site.port))
        port = await p.start()
        try:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"CONNECT shop.acme.test:{site.port} HTTP/1.1\r\n\r\n".encode())
            await w.drain()
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
            w.write(first)
            await w.drain()
            try:
                rest = await asyncio.wait_for(r.read(65536), 1.5)
            except asyncio.TimeoutError:
                rest = b"(still open)"                          # the site has nothing to say to a TLS hello; the tunnel stands
            w.close()
        finally:
            await p.stop()
    return head + rest, stand_in.asked, seen, blocked


HELLO = b"\x16\x03\x01\x00\x05hello"          # how a TLS connection begins: a handshake record


async def test_a_tls_connection_of_the_browser_is_checked_before_caido_gets_it(dns, monkeypatch):
    reply, asked, seen, blocked = await through_the_browsers_proxy(dns, monkeypatch, HELLO)
    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\n(still open)" and len(seen) == 1
    assert seen[0][:2] == ("shop.acme.test", "127.0.0.1") and [m for m, _ in asked] == ["CONNECT"] and blocked == []


async def test_a_bad_certificate_closes_the_browsers_connection_and_caido_never_sees_it(dns, monkeypatch):
    reply, asked, seen, blocked = await through_the_browsers_proxy(dns, monkeypatch, HELLO, bad="self-signed certificate")
    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\n"            # the tunnel was granted, and closed without a byte
    assert asked == [] and len(seen) == 1
    assert blocked == ["the certificate of shop.acme.test is not valid (self-signed certificate). Nothing was sent."]


async def test_a_connection_that_is_not_tls_is_not_held_up(dns, monkeypatch):
    """A plain WebSocket goes through a tunnel too. There is no certificate in it to check."""
    reply, asked, seen, _ = await through_the_browsers_proxy(dns, monkeypatch, b"GET /chat HTTP/1.1\r\nHost: shop.acme.test\r\n\r\n", bad="it must not be asked")
    assert seen == [] and [m for m, _ in asked] == ["CONNECT"] and b"site says: shop.acme.test/chat" in reply


async def test_without_caido_the_browsers_proxy_is_what_it_was(dns, monkeypatch):
    reply, asked, seen, _ = await through_the_browsers_proxy(dns, monkeypatch, HELLO, bad="it must not be asked", verify=False)
    assert seen == [] and [m for m, _ in asked] == ["CONNECT"] and reply.endswith(b"(still open)")


async def test_a_session_through_caido_has_its_proxy_check_certificates(conn, monkeypatch):
    a_caido(monkeypatch)
    chat, _ = await seeded_run(conn)
    made = []

    class Camoufox:
        def __init__(self, **kw):
            raise RuntimeError("no browser in this test")
    real = browser_proxy.Proxy

    def proxy(*a, **kw):
        made.append(real(*a, **kw))
        return made[-1]
    monkeypatch.setattr(browser_proxy, "Proxy", proxy)
    monkeypatch.setitem(__import__("sys").modules, "camoufox.async_api", type("M", (), {"AsyncCamoufox": Camoufox}))
    with pytest.raises(ToolError, match="the browser could not start"):
        await browser._launch(chat.id)                         # noqa: SLF001
    assert made[0].verify_sites is True and made[0].via.address == "127.0.0.1:18791"
    await settings.put(conn, "browser_via_caido", False)
    with pytest.raises(ToolError, match="the browser could not start"):
        await browser._launch(chat.id)                         # noqa: SLF001
    assert made[1].verify_sites is False
