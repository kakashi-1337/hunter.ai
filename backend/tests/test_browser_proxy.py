import asyncio

import pytest

from hunterai.upstream import Upstream
from hunterai import browser_proxy
from hunterai.tools.base import ToolError


@pytest.fixture
async def upstream():
    """A tiny local server standing in for a site; it records what it was sent."""
    seen = []

    async def handle(r, w):
        head = await r.readuntil(b"\r\n\r\n")
        seen.append(head.decode())
        w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await w.drain(); w.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1], seen
    server.close(); await server.wait_closed()


async def ask(port, raw: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw); await w.drain()
    data = await asyncio.wait_for(r.read(65536), 5)
    w.close()
    return data


async def test_check_refuses_local_addresses_own_api_and_names_that_resolve_locally(dns):
    table, lookups = dns
    table.update({"acme.test": ["93.184.216.34"], "rebind.test": [["93.184.216.34", "127.0.0.1"]], "intranet.test": ["10.0.0.5"]})
    p = browser_proxy.Proxy()
    assert await p.check("acme.test", 443) == "93.184.216.34" and await p.check("ACME.test.", 443) == "93.184.216.34"
    for host, port in (("127.0.0.1", 6333), ("localhost", 6333), ("rebind.test", 80), ("intranet.test", 80), ("[::1]", 80), ("169.254.169.254", 80), ("2130706433", 80), ("0x7f.1", 80)):
        with pytest.raises(ToolError, match="this machine or local network|does not resolve|cannot resolve|no such host|unusable"):
            await p.check(host, port)
    for host in ("127.0.0.1", "localhost"):
        with pytest.raises(ToolError, match="HunterAI's own address"):
            await p.check(host, 8790)
    p.local_ok.add(("localhost", 3000))
    assert await p.check("localhost", 3000) == "127.0.0.1"
    with pytest.raises(ToolError, match="localhost:6333 is an address"):       # allowing one port opens no other
        await p.check("localhost", 6333)
    with pytest.raises(ToolError, match="own address"):
        p.local_ok.add(("127.0.0.1", 8790)); await p.check("127.0.0.1", 8790)


async def test_requests_to_local_services_are_refused_and_reported(upstream):
    port, seen = upstream
    blocked = []
    p = browser_proxy.Proxy(blocked.append); proxy_port = await p.start()
    try:
        got = await ask(proxy_port, f"GET http://127.0.0.1:{port}/secret HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert got.startswith(b"HTTP/1.1 403") and b"this machine or local network" in got and seen == []
        got = await ask(proxy_port, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert got.startswith(b"HTTP/1.1 403") and seen == [] and len(blocked) == 2
        got = await ask(proxy_port, b"GET ftp://example.com/x HTTP/1.1\r\n\r\n")
        assert got.startswith(b"HTTP/1.1 403")
    finally:
        await p.stop()


async def test_an_allowed_local_site_is_reached_one_request_per_connection(upstream):
    port, seen = upstream
    p = browser_proxy.Proxy(); proxy_port = await p.start()
    p.local_ok.add(("127.0.0.1", port))
    try:
        got = await ask(proxy_port, f"GET http://127.0.0.1:{port}/page?x=1 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nProxy-Connection: keep-alive\r\nCookie: a=b\r\n\r\n".encode())
        assert got.endswith(b"hello")
        sent = seen[0]
        assert sent.startswith("GET /page?x=1 HTTP/1.1\r\n") and "Cookie: a=b" in sent and "Connection: close" in sent and "Proxy-Connection" not in sent
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode()); await w.drain()
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        w.write(b"GET /tunnel HTTP/1.1\r\nHost: x\r\n\r\n"); await w.drain()
        assert (await asyncio.wait_for(r.read(65536), 5)).endswith(b"hello") and seen[1].startswith("GET /tunnel")
        w.close()
    finally:
        await p.stop()


async def test_with_a_carrying_proxy_checked_connections_are_handed_to_it_by_name(dns):
    table, _ = dns
    table.update({"acme.test": ["93.184.216.34"], "intranet.test": ["10.0.0.5"]})
    seen = []

    async def carrier(r, w):                       # stands in for Caido
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        seen.append(head)
        if head.startswith("CONNECT refused.test"):
            w.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        elif head.startswith("CONNECT"):
            w.write(b"HTTP/1.1 200 Connection Established\r\n\r\n"); await w.drain()
            w.write(b"tunnel:" + await r.read(5))
        else:
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await w.drain(); w.close()
    server = await asyncio.start_server(carrier, "127.0.0.1", 0)
    p = browser_proxy.Proxy()
    p.via = Upstream("127.0.0.1", server.sockets[0].getsockname()[1])
    port = await p.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT acme.test:443 HTTP/1.1\r\nHost: acme.test:443\r\n\r\n"); await w.drain()
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        w.write(b"hello"); await w.drain()
        assert await asyncio.wait_for(r.read(100), 5) == b"tunnel:hello"
        w.close()
        assert seen[0].startswith("CONNECT acme.test:443 HTTP/1.1\r\nHost: acme.test:443")       # the name, for the carrier to record
        plain = await ask(port, b"GET http://acme.test/a?b=1 HTTP/1.1\r\nHost: acme.test\r\nProxy-Connection: keep-alive\r\n\r\n")
        assert plain.endswith(b"ok") and seen[1].startswith("GET http://acme.test/a?b=1 HTTP/1.1\r\n") and "Proxy-Connection" not in seen[1]
        blocked = await ask(port, b"CONNECT intranet.test:443 HTTP/1.1\r\nHost: intranet.test:443\r\n\r\n")
        assert blocked.startswith(b"HTTP/1.1 403") and len(seen) == 2                                 # refused here: the carrier never hears of it
        table["refused.test"] = ["93.184.216.35"]
        assert (await ask(port, b"CONNECT refused.test:443 HTTP/1.1\r\nHost: refused.test:443\r\n\r\n")).startswith(b"HTTP/1.1 502")
    finally:
        await p.stop()
        server.close(); await server.wait_closed()


async def _through(proxy, request: bytes) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(request)
    await writer.drain()
    answer = (await reader.read()).decode("latin-1")
    writer.close()
    return answer


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_carrying_proxy_with_a_login_is_given_it_for_plain_requests_and_for_tunnels(kind, dns):
    from tests.proxies import HttpProxy, Site, Socks5Proxy, running
    dns[0]["shop.acme.test"] = ["203.0.113.5"]
    site = Site()
    carrier = (HttpProxy if kind == "http" else Socks5Proxy)(site, "hunter", "s3cret")
    p = browser_proxy.Proxy()
    async with running(site, carrier):
        p.via = Upstream("127.0.0.1", carrier.port, scheme=kind, user="hunter", password="s3cret")
        await p.start()
        try:
            plain = await _through(p, b"GET http://shop.acme.test/cart?x=1 HTTP/1.1\r\nHost: shop.acme.test\r\n\r\n")
            assert "site says: shop.acme.test/cart?x=1" in plain
            reader, writer = await asyncio.open_connection("127.0.0.1", p.port)
            writer.write(b"CONNECT shop.acme.test:443 HTTP/1.1\r\nHost: shop.acme.test:443\r\n\r\n")
            assert b" 200 " in await reader.readuntil(b"\r\n\r\n")
            writer.write(b"GET /inside HTTP/1.1\r\nHost: shop.acme.test\r\nConnection: close\r\n\r\n")
            assert "site says: shop.acme.test/inside" in (await reader.read()).decode()
            writer.close()
        finally:
            await p.stop()
    named = [a for a in carrier.asked]
    assert named == ([("GET", "http://shop.acme.test/cart?x=1"), ("CONNECT", "shop.acme.test:443")] if kind == "http"
                     else [("shop.acme.test", 80), ("shop.acme.test", 443)])
    assert "Proxy-Authorization" not in str(site.requests) and len(site.requests) == 2      # the login stays with the proxy


async def test_a_carrying_proxy_that_refuses_the_login_is_named_in_the_refusal(dns):
    from tests.proxies import Site, Socks5Proxy, running
    dns[0]["shop.acme.test"] = ["203.0.113.5"]
    site, blocked = Site(), []
    carrier = Socks5Proxy(site, "hunter", "s3cret")
    p = browser_proxy.Proxy(on_block=blocked.append)
    async with running(site, carrier):
        p.via = Upstream("127.0.0.1", carrier.port, scheme="socks5", user="hunter", password="wrong")
        await p.start()
        try:
            answer = await _through(p, b"GET http://shop.acme.test/ HTTP/1.1\r\nHost: shop.acme.test\r\n\r\n")
        finally:
            await p.stop()
    assert answer.startswith("HTTP/1.1 502") and "refused the user name or the password" in answer
    assert site.requests == [] and "wrong" not in answer and blocked and "wrong" not in blocked[0]
