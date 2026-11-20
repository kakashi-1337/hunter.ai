"""The local hop: a plain HTTP proxy on loopback that carries to the owner's proxy, with its login."""
import asyncio

import httpx
import pytest

from hunterai import hop, upstream
from tests.proxies import HttpProxy, Site, Socks5Proxy, running


def _carrier(kind, site, user="hunter", password="s3cret"):
    return (HttpProxy if kind == "http" else Socks5Proxy)(site, user, password)


def _up(kind, carrier, password="s3cret"):
    return upstream.Upstream("127.0.0.1", carrier.port, scheme=kind, user="hunter", password=password)


async def _raw(address: str, request: bytes) -> str:
    host, port = address.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(request)
    await writer.drain()
    answer = (await reader.read()).decode("latin-1")
    writer.close()
    return answer


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_client_that_has_no_login_is_carried_to_a_proxy_that_wants_one(kind):
    site = Site()
    carrier = _carrier(kind, site)
    async with running(site, carrier):
        address = await hop.address(_up(kind, carrier))
        assert address.startswith("http://127.0.0.1:") and "hunter" not in address and "s3cret" not in address
        async with httpx.AsyncClient(proxy=address, trust_env=False) as client:
            r = await client.get("http://shop.acme.test/cart?x=1")           # a name nothing here can look up: the proxy does
        assert (r.status_code, r.text) == (200, "site says: shop.acme.test/cart?x=1")
        reader, writer = await asyncio.open_connection("127.0.0.1", int(address.rsplit(":", 1)[1]))
        writer.write(b"CONNECT shop.acme.test:443 HTTP/1.1\r\nHost: shop.acme.test:443\r\n\r\n")       # what a client does for https
        assert b" 200 " in await reader.readuntil(b"\r\n\r\n")
        writer.write(b"GET /inside HTTP/1.1\r\nHost: shop.acme.test\r\nConnection: close\r\n\r\n")
        assert "site says: shop.acme.test/inside" in (await reader.read()).decode()
        writer.close()
    assert carrier.asked == ([("GET", "http://shop.acme.test/cart?x=1"), ("CONNECT", "shop.acme.test:443")] if kind == "http"
                             else [("shop.acme.test", 80), ("shop.acme.test", 443)])
    assert len(site.heads) == 2 and not any("proxy-authorization" in head for head in site.heads)     # the login stays with the proxy


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_proxy_that_refuses_the_login_is_named_as_the_reason(kind):
    site = Site()
    carrier = _carrier(kind, site)
    async with running(site, carrier):
        up = _up(kind, carrier, password="wrong-one")
        address = await hop.address(up)
        tunnel = await _raw(address, b"CONNECT shop.acme.test:443 HTTP/1.1\r\nHost: shop.acme.test:443\r\n\r\n")
        with pytest.raises(httpx.ProxyError, match="refused the user name or the password"):   # an https client is told why
            async with httpx.AsyncClient(proxy=address, trust_env=False) as client:
                await client.get("https://shop.acme.test/")
        plain = await _raw(address, b"GET http://shop.acme.test/ HTTP/1.1\r\nHost: shop.acme.test\r\n\r\n")
    assert tunnel.startswith("HTTP/1.1 502 the proxy refused the user name or the password\r\n")
    assert f"{hop.REFUSED}: the proxy refused the user name or the password" in tunnel
    if kind == "socks5":                       # an HTTP proxy answers a plain request itself, with its own 407
        assert plain.startswith("HTTP/1.1 502 ") and f"{hop.REFUSED}: the proxy refused the user name or the password" in plain
    else:
        assert plain.startswith("HTTP/1.1 407 ")
    assert site.requests == [] and "wrong-one" not in tunnel + plain
    count, why = hop.refused(up)
    assert count >= 2 and "refused the user name or the password" in why and "wrong-one" not in why


async def test_nothing_is_connected_directly_when_the_proxy_is_not_there():
    site = Site()
    async with running(site):
        gone = site.port                       # in use a moment ago, free again after the block
    up = upstream.Upstream("127.0.0.1", gone)
    address = await hop.address(up)
    answer = await _raw(address, f"GET http://127.0.0.1:{gone}/ HTTP/1.1\r\nHost: 127.0.0.1:{gone}\r\n\r\n".encode())
    assert answer.startswith("HTTP/1.1 502 nothing answers at the proxy's address") and hop.refused(up)[0] == 1


async def test_one_hop_serves_one_proxy_until_the_app_stops():
    one, two = upstream.Upstream("127.0.0.1", 8080), upstream.Upstream("127.0.0.1", 8080, user="hunter", password="s3cret")
    first, again, other = await asyncio.gather(hop.address(one), hop.address(one), hop.address(two))
    assert first == again != other and hop.refused(one) == (0, "")
    await hop.shutdown()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", int(first.rsplit(":", 1)[1]))
    assert (await hop.address(one)).startswith("http://127.0.0.1:")          # asked for again: started again
