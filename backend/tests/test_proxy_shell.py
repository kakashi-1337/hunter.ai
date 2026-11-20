"""Commands of a chat whose target has a proxy are given the way to it, and the model is told how it works."""
import shutil

import pytest

from hunterai import upstream
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import settings, targets
from hunterai.tools import registry, shell
from tests.proxies import HttpProxy, Site, Socks5Proxy, running
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, fake_tool, run_call, seeded_run

ALLOW = {"risk": {"execute": "allow"}}
NAMES = ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "all_proxy", "http_proxy", "https_proxy", "no_proxy")
SHOW = "env | grep -E '^(ALL|HTTPS?|NO)_PROXY=|^(all|https?|no)_proxy=' | LC_ALL=C sort"
# curl reads the lower-case http_proxy only, as many programs do. A URL is all the command names: a word like
# urllib.request would be taken for a host outside the scope, and the call would wait for the owner's answer.
FETCH = "curl -sS --max-time 10 http://shop.acme.test/x"
pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")


@pytest.fixture(autouse=True)
def shell_registered(monkeypatch):
    for t in shell.tools():
        registry.register(t)
    for name in NAMES:                       # whatever proxy this machine itself is behind is no part of a test
        monkeypatch.delenv(name, raising=False)


async def run(conn, command, proxy=None, default=None, scope=None):
    """`command` in a chat on a target with `proxy` as its own, `default` being the one in Settings."""
    chat, run_id = await seeded_run(conn, scope=scope or {"in": ["*.acme.test"], "out": []}, permissions=ALLOW)
    if proxy:
        await upstream.put_target(conn, chat.target_id, proxy)
    if default:
        await settings.put(conn, "upstream_proxy", default)
    # A short limit: with the proxy variables missing, the command would try the network of this machine itself.
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "execute", {"command": command, "timeout": 15}))
    return out.block.content


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_a_command_is_given_the_way_to_the_targets_proxy_and_never_its_login(conn, kind):
    site = Site()
    carrier = (HttpProxy if kind == "http" else Socks5Proxy)(site, "hunter", "s3cret-pass")
    async with running(site, carrier):
        text = await run(conn, f"{SHOW}; {FETCH}", proxy=f"{kind}://hunter:s3cret-pass@127.0.0.1:{carrier.port}")
    lines = text.splitlines()
    assert lines[0] == "exit code: 0" and "site says: shop.acme.test/x" in lines, text
    told = dict(line.split("=", 1) for line in lines if line.split("=", 1)[0] in NAMES)
    assert sorted(told) == sorted(NAMES)
    hop_at = told["HTTP_PROXY"]
    assert hop_at.startswith("http://127.0.0.1:") and hop_at != f"http://127.0.0.1:{carrier.port}"
    assert {told[n] for n in NAMES if "NO" not in n.upper()} == {hop_at}             # every spelling a program may read
    assert told["NO_PROXY"] == told["no_proxy"] == "localhost,127.0.0.1,::1"
    assert "s3cret-pass" not in text and "hunter" not in text                        # the command sees an address on loopback, nothing else
    assert carrier.asked == ([("GET", "http://shop.acme.test/x")] if kind == "http" else [("shop.acme.test", 80)])


async def test_the_proxy_from_settings_serves_a_target_that_has_none_and_no_proxy_means_no_variables(conn):
    site = Site()
    carrier = HttpProxy(site)
    async with running(site, carrier):
        assert not [line for line in (await run(conn, SHOW)).splitlines() if line.split("=", 1)[0] in NAMES]
        text = await run(conn, f"{SHOW}; {FETCH}", default=f"http://127.0.0.1:{carrier.port}")
    assert "HTTP_PROXY=http://127.0.0.1:" in text and "site says: shop.acme.test/x" in text
    assert carrier.asked == [("GET", "http://shop.acme.test/x")]


async def test_a_proxy_that_does_not_carry_is_named_under_the_commands_output(conn):
    site = Site()
    carrier = Socks5Proxy(site, "hunter", "s3cret-pass")
    async with running(site, carrier):
        text = await run(conn, f"{FETCH}; echo after", proxy=f"socks5://hunter:wrong-pass-1@127.0.0.1:{carrier.port}")
    assert site.requests == [] and "after" in text.splitlines() and "wrong-pass-1" not in text
    assert text.rstrip().endswith(
        f"[the proxy at 127.0.0.1:{carrier.port} did not carry 1 connection while this command ran: the proxy refused "
        "the user name or the password. Nothing was sent directly.]")


async def test_a_call_that_carries_the_proxys_login_is_refused_and_one_that_names_a_word_is_not(conn):
    chat, run_id = await seeded_run(conn, scope={"in": ["*.acme.test"], "out": []}, permissions=ALLOW)
    await upstream.put_target(conn, chat.target_id, "http://me:password@127.0.0.1:8080")
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", "execute", {"command": "echo me:password@127.0.0.1:8080"}))
    assert out.block.is_error and "permission policy (secret)" in out.block.content and "the login of a proxy" in out.block.content
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c2", "execute", {"command": "echo the password field"}))
    assert not out.block.is_error and "the password field" in out.block.content


def test_the_model_is_told_how_the_proxy_works_and_never_its_login():
    up = upstream.Upstream("fr.example.net", 1080, scheme="socks5", user="hunter", password="s3cret-pass")
    told = upstream.rule(up, ["execute", "web_fetch", "browser_open"])
    assert "fr.example.net:1080" in told and "HTTP_PROXY" in told and "nmap" in told and "Nothing is sent directly" in told
    assert "hunter" not in told and "s3cret-pass" not in told
    quiet = upstream.rule(up, ["web_fetch"])                    # no shell: nothing to say about commands
    assert "fr.example.net:1080" in quiet and "HTTP_PROXY" not in quiet and "nmap" not in quiet
    assert upstream.rule(None, ["execute"]) == "" and upstream.rule(up, []) == ""


async def test_a_run_tells_the_model_about_the_proxy_of_its_target(conn, setup):
    tool, _ = fake_tool("t_echo"); registry.register(tool)
    with_proxy = await targets.create(conn, "french program", "", {"in": ["*.acme.test"], "out": []})
    without = await targets.create(conn, "other", "", {"in": ["*.other.test"], "out": []})
    await upstream.put_target(conn, with_proxy.id, "socks5://hunter:s3cret-pass@fr.example.net:1080")
    systems = []
    for target in (with_proxy, without):
        p = Turns("abliteration", [TextDelta("ok")])
        use(abliteration=p)
        await go(conn, *await setup(target_id=target.id))
        systems.append(p.calls[0]["system"])
    assert "the owner's proxy at fr.example.net:1080" in systems[0] and "s3cret-pass" not in systems[0]
    assert "proxy" not in systems[1]
