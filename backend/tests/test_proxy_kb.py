"""Knowledge crawls go through the owner's proxy when, and only when, the owner switched that on."""
import pytest

from hunterai import upstream
from hunterai.kb import crawl
from hunterai.kb.limits import Budget
from hunterai.repo import settings
from hunterai.tools.base import ToolError
from tests.proxies import HttpProxy, Site, Socks5Proxy, running

IP = "203.0.113.5"        # never routed: a request that went out by mistake reaches no one


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    async def none(_):
        return None
    monkeypatch.setattr(crawl, "_sleep", none)


def _carrier(kind, site):
    return (HttpProxy if kind == "http" else Socks5Proxy)(site, "hunter", "s3cret-pass")


def _up(kind, carrier, password="s3cret-pass"):
    return upstream.Upstream("127.0.0.1", carrier.port, scheme=kind, user="hunter", password=password)


async def test_crawls_take_the_proxy_only_when_their_switch_is_on(conn):
    assert await upstream.for_crawls() is None
    await settings.put(conn, "upstream_proxy", "socks5://hunter:s3cret-pass@fr.example.net:1080")
    assert await upstream.for_crawls() is None                   # set for the work on targets: a crawl still connects itself
    await settings.put(conn, "upstream_proxy_kb", True)
    assert (await upstream.for_crawls()).address == "fr.example.net:1080"
    await settings.put(conn, "upstream_proxy", "")
    assert await upstream.for_crawls() is None


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_page_is_fetched_through_the_proxy_by_name_after_the_host_was_checked_here(dns, kind):
    dns[0]["docs.example.test"] = [IP]; dns[0]["intranet.test"] = ["10.0.0.5"]
    site = Site()
    carrier = _carrier(kind, site)
    async with running(site, carrier):
        f = crawl.Fetcher()
        f.via = _up(kind, carrier)
        try:
            status, _, body, cut = await f.get("http://docs.example.test/guide")
            with pytest.raises(ToolError, match="this machine or local network"):      # the proxy is not a way round this rule
                await f.get("http://intranet.test/")
        finally:
            await f.close()
    assert (status, body, cut) == (200, b"site says: docs.example.test/guide", False)
    assert carrier.asked == ([("GET", "http://docs.example.test/guide")] if kind == "http" else [("docs.example.test", 80)])
    assert dns[1] == ["docs.example.test", "intranet.test"]


async def test_a_site_the_owner_says_is_local_is_fetched_directly_whatever_the_proxy():
    site = Site()
    carrier = HttpProxy(site)
    async with running(site, carrier):
        f = crawl.Fetcher(allow_local=True)
        f.via = _up("http", carrier)
        try:
            status, _, body, _ = await f.get(f"http://127.0.0.1:{site.port}/notes")
        finally:
            await f.close()
    assert status == 200 and body == f"site says: 127.0.0.1:{site.port}/notes".encode() and carrier.asked == []


async def test_a_proxy_that_does_not_carry_fails_the_page_and_nothing_is_fetched_directly(dns):
    dns[0]["docs.example.test"] = [IP]
    site = Site()
    carrier = _carrier("socks5", site)
    async with running(site, carrier):
        f = crawl.Fetcher()
        f.via = _up("socks5", carrier, password="wrong-pass-1")
        try:
            with pytest.raises(ToolError) as e:
                await f.get("http://docs.example.test/guide")
        finally:
            await f.close()
    assert str(e.value) == (f"the proxy at 127.0.0.1:{carrier.port} did not carry it: the proxy refused the user name or "
                            "the password")
    assert site.requests == []


async def test_a_crawl_asks_for_the_proxy_itself(conn, dns, tmp_path):
    dns[0]["docs.example.test"] = [IP]
    site = Site(lambda host, path: "A page of the manual. " * 40)
    carrier = _carrier("http", site)
    async with running(site, carrier):
        await settings.put(conn, "upstream_proxy", f"http://hunter:s3cret-pass@127.0.0.1:{carrier.port}")
        await settings.put(conn, "upstream_proxy_kb", True)
        got = [x async for x in crawl.crawl({"url": "http://docs.example.test/manual.txt", "max_pages": 1, "robots": False},
                                            tmp_path / "web", Budget())]
    assert [type(x).__name__ for x in got][:1] == ["Found"] and ("GET", "http://docs.example.test/manual.txt") in carrier.asked
    assert len(carrier.asked) == len(site.requests)              # every request of the crawl went through the proxy
