"""A target's own proxy, the proxy test, and the proxy settings, as the app's API gives them."""
import pytest

from hunterai import hop, upstream
from tests.proxies import HttpProxy, Site, Socks5Proxy, running
from tests.test_api import client  # noqa: F401

FR = "socks5://hunter:s3cret-pass@fr.example.net:1080"
SHOWN = "socks5://hunter@fr.example.net:1080"


async def test_a_target_keeps_a_proxy_of_its_own(client, conn):
    t = (await client.post("/api/targets", json={"name": "french program", "proxy": f" {FR} "})).json()
    assert (t["proxy"], t["proxy_password"]) == (SHOWN, True) and "s3cret-pass" not in str(t)
    assert (await upstream.of_target(conn, t["id"])).password == "s3cret-pass"
    other = (await client.post("/api/targets", json={"name": "no proxy"})).json()
    assert (other["proxy"], other["proxy_password"]) == ("", False)
    listed = (await client.get("/api/targets")).json()
    assert [(x["name"], x["proxy"], x["proxy_password"]) for x in listed] == [("french program", SHOWN, True), ("no proxy", "", False)]
    assert "s3cret-pass" not in str(listed)

    path = f"/api/targets/{t['id']}"
    t = (await client.patch(path, json={"notes": "n", "proxy": SHOWN})).json()            # the form sends back what it was shown
    assert t["proxy_password"] is True and (await upstream.of_target(conn, t["id"])).password == "s3cret-pass"
    t = (await client.patch(path, json={"notes": "m"})).json()                             # not named: left as it is
    assert t["proxy"] == SHOWN and t["notes"] == "m"
    bad = await client.patch(path, json={"notes": "changed", "proxy": "fr.example.net"})
    assert bad.status_code == 422 and "proxy" in bad.text and "port" in bad.text
    assert (await client.get("/api/targets")).json()[0]["notes"] == "m"                    # refused as a whole
    t = (await client.patch(path, json={"proxy": "socks5://someone@fr.example.net:1080"})).json()     # another user: the old password is not theirs
    assert t["proxy_password"] is False
    t = (await client.patch(path, json={"proxy": ""})).json()
    assert (t["proxy"], t["proxy_password"]) == ("", False) and await upstream.of_target(conn, t["id"]) is None

    bad = await client.post("/api/targets", json={"name": "x", "proxy": "socks4://h:1"})
    assert bad.status_code == 422 and "socks5://" in bad.text
    assert len((await client.get("/api/targets")).json()) == 2                             # and nothing was created


async def test_the_proxy_test_says_what_sites_see(client, monkeypatch):
    site = Site(lambda host, path: "fl=1\nip=185.1.2.3\nts=1\nloc=FR\n" if path == "/trace" else "<html>hello</html>")
    carrier = Socks5Proxy(site, "hunter", "s3cret-pass")
    monkeypatch.setenv("HUNTERAI_PROXY_TEST_URL", "http://first.test/page, http://probe.test/trace")
    test = "/api/settings/proxy/test"
    async with running(site, carrier):
        typed = f"socks5://hunter:s3cret-pass@127.0.0.1:{carrier.port}"
        shown = f"socks5://hunter@127.0.0.1:{carrier.port}"
        r = (await client.post(test, json={"address": typed})).json()                      # an address that is not saved yet
        assert r["ok"] is True and (r["ip"], r["country"], r["through"]) == ("185.1.2.3", "FR", f"127.0.0.1:{carrier.port}")
        assert isinstance(r["ms"], int) and "s3cret-pass" not in str(r)
        assert carrier.asked == [("first.test", 80), ("probe.test", 80)]                   # the first could not say: the next was asked

        none = await client.post(test, json={})
        assert none.status_code == 400 and "no proxy" in none.text.lower()
        await client.put("/api/settings", json={"upstream_proxy": typed})
        assert (await client.post(test, json={"address": shown})).json()["ok"] is True     # as the form shows it: the stored password is used
        assert (await client.post(test, json={})).json()["country"] == "FR"                # the one that is in effect

        t = (await client.post("/api/targets", json={"name": "fr", "proxy": typed})).json()
        await client.put("/api/settings", json={"upstream_proxy": ""})
        assert (await client.post(test, json={"target_id": t["id"], "address": t["proxy"]})).json()["ok"] is True
        assert (await client.post(test, json={"target_id": t["id"]})).json()["ok"] is True

        await client.put("/api/settings", json={"provider_proxy": typed})                  # the one for the model providers
        assert (await client.post(test, json={"address": shown})).json()["ok"] is False    # not the default: no password is known for it
        assert (await client.post(test, json={"address": shown, "providers": True})).json()["ok"] is True
        assert (await client.post(test, json={"providers": True})).json()["country"] == "FR"

        wrong = (await client.post(test, json={"address": f"socks5://hunter:nope-nope-nope@127.0.0.1:{carrier.port}"})).json()
        assert wrong["ok"] is False and "user name or the password" in wrong["error"] and "nope-nope-nope" not in str(wrong)
        assert wrong["through"] == f"127.0.0.1:{carrier.port}"
    gone = (await client.post(test, json={"address": typed})).json()                       # the proxy has stopped
    assert gone["ok"] is False and "nothing answers at the proxy's address" in gone["error"]
    bad = await client.post(test, json={"address": "nonsense"})
    assert bad.status_code == 400 and "port" in bad.text


@pytest.mark.parametrize("body, found", [
    ("fl=29f1\nh=www.cloudflare.com\nip=2a01:e0a:1::5\nloc=fr\nwarp=off\n", ("2a01:e0a:1::5", "FR")),
    ('{"ip": "185.1.2.3"}', ("185.1.2.3", "")),
    ('{"ip": "185.1.2.3", "country": "fr", "loc": "48.85,2.35", "org": "AS1 Example"}', ("185.1.2.3", "FR")),
    ('{"country": "DE", "asn": {"asnum": 1}, "geo": {"city": "Berlin"}}', ("", "DE")),
    ("<html>ip=not-an-address</html>", ("", "")), ('["185.1.2.3"]', ("", "")), ("", ("", "")),
])
def test_what_a_test_page_says_is_read(body, found):
    assert hop.where(body) == found


async def test_the_proxy_settings_for_knowledge_and_for_models(client):
    s = (await client.get("/api/settings")).json()
    assert (s["upstream_proxy_kb"], s["provider_proxy"], s["provider_proxy_password"]) == (False, "", False)
    s = (await client.put("/api/settings", json={"upstream_proxy_kb": True, "provider_proxy": "http://me:long-password@proxy.example.net:3128"})).json()
    assert (s["upstream_proxy_kb"], s["provider_proxy"], s["provider_proxy_password"]) == (True, "http://me@proxy.example.net:3128", True)
    assert "long-password" not in str(s) and upstream.of_providers().password == "long-password"
    s = (await client.put("/api/settings", json={"provider_proxy": "http://me@proxy.example.net:3128"})).json()      # sent back as shown
    assert s["provider_proxy_password"] is True and upstream.of_providers().password == "long-password"
    bad = await client.put("/api/settings", json={"provider_proxy": "proxy.example.net", "upstream_proxy_kb": False})
    assert bad.status_code == 400 and "port" in bad.text
    assert (await client.get("/api/settings")).json()["upstream_proxy_kb"] is True          # refused as a whole
    s = (await client.put("/api/settings", json={"provider_proxy": "", "upstream_proxy_kb": False})).json()
    assert (s["upstream_proxy_kb"], s["provider_proxy"], s["provider_proxy_password"]) == (False, "", False) and upstream.of_providers() is None
