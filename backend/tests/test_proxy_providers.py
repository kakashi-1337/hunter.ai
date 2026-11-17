"""The model providers are reached through a proxy of their own when the owner set one."""
import json

import pytest

from hunterai import upstream
from hunterai.providers import anthropic as anthropic_provider
from hunterai.providers import base, claude_code, codex, google, openai_compat
from hunterai.providers.base import Options, ProviderError, TextDelta, WireMessage
from hunterai.types import TextBlock
from tests.proxies import HttpProxy, Site, Socks5Proxy, running

ASK = [WireMessage("user", [TextBlock(text="hi")])]
PRINTS_ITS_ENVIRONMENT = '#!/bin/sh\ncat >/dev/null\nenv > "$FAKE_OUT/env.txt"\n'


def _set(monkeypatch, carrier, kind="http"):
    monkeypatch.setenv(upstream.PROVIDER_ENV, f"{kind}://me:long-password@127.0.0.1:{carrier.port}")


async def test_the_way_to_the_providers_is_a_hop_that_holds_the_login(monkeypatch):
    assert await base.way_out() is None
    monkeypatch.setenv(upstream.PROVIDER_ENV, "socks5://me:long-password@proxy.example.net:1080")
    at = await base.way_out()
    assert at.startswith("http://127.0.0.1:") and "long-password" not in at and "me@" not in at
    monkeypatch.setenv(upstream.PROVIDER_ENV, "proxy.example.net")
    with pytest.raises(ProviderError, match=upstream.PROVIDER_ENV) as e:
        await base.way_out()
    assert e.value.kind == "unavailable"


@pytest.mark.parametrize("kind", ["http", "socks5"])
async def test_an_openai_compatible_provider_is_asked_through_the_proxy(monkeypatch, kind):
    def answer(host, path):
        if path == "/v1/models":
            return json.dumps({"data": [{"id": "m1"}]})
        return 'data: {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
    site = Site(answer, "application/json")
    carrier = (HttpProxy if kind == "http" else Socks5Proxy)(site, "me", "long-password")
    monkeypatch.setenv("X_API_KEY", "key-1")
    p = openai_compat.OpenAICompatProvider("x", "http://api.models.test/v1", "X_API_KEY")
    async with running(site, carrier):
        _set(monkeypatch, carrier, kind)
        assert [m.id for m in await p.models()] == ["m1"]
        events = [e async for e in p.stream("m1", "", ASK, Options())]
    assert TextDelta("hello") in events
    assert [r[:2] for r in site.requests] == [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
    assert carrier.asked == ([("GET", "http://api.models.test/v1/models"), ("POST", "http://api.models.test/v1/chat/completions")]
                             if kind == "http" else [("api.models.test", 80)] * 2)
    assert not any("proxy-authorization" in head for head in site.heads)


async def test_a_provider_whose_proxy_does_not_carry_is_unavailable_and_is_not_asked_directly(monkeypatch):
    site = Site()
    carrier = Socks5Proxy(site, "me", "long-password")
    monkeypatch.setenv("X_API_KEY", "key-1")
    p = openai_compat.OpenAICompatProvider("x", "http://api.models.test/v1", "X_API_KEY")
    async with running(site, carrier):
        monkeypatch.setenv(upstream.PROVIDER_ENV, f"socks5://me:wrong-password-1@127.0.0.1:{carrier.port}")
        with pytest.raises(ProviderError) as e:
            [x async for x in p.stream("m1", "", ASK, Options())]
    assert e.value.kind == "unavailable" and "the proxy refused the user name or the password" in str(e.value)
    assert site.requests == [] and "wrong-password-1" not in str(e.value)


@pytest.mark.parametrize("which", ["anthropic", "google"])
async def test_the_client_of_an_sdk_is_pointed_at_the_proxy(monkeypatch, which):
    site = Site(lambda host, path: json.dumps({"data": [], "models": [], "has_more": False}), "application/json")
    carrier = HttpProxy(site, "me", "long-password")
    if which == "anthropic":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://models.example.test")
        provider = anthropic_provider.AnthropicProvider()
    else:
        monkeypatch.setenv("GEMINI_API_KEY", "g-test")
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "http://models.example.test")
        provider = google.GoogleProvider()
    async with running(site, carrier):
        _set(monkeypatch, carrier)
        await provider.models()                  # an empty list: the provider answers with the models it knows
    assert carrier.asked and {(method, where.split("/v1")[0]) for method, where in carrier.asked} == {("GET", "http://models.example.test")}


@pytest.mark.parametrize("module, exe", [(claude_code, "claude"), (codex, "codex")])
async def test_a_cli_provider_is_given_the_way_to_the_proxy_and_never_its_login(monkeypatch, tmp_path, module, exe):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / exe).write_text(PRINTS_ITS_ENVIRONMENT)
    (bindir / exe).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_OUT", str(tmp_path))
    monkeypatch.setenv(upstream.PROVIDER_ENV, "socks5://me:long-password@proxy.example.net:1080")
    try:
        [e async for e in module.make().stream("m", "", ASK, Options())]
    except ProviderError:
        pass                                     # the stand-in prints no answer
    env = dict(line.split("=", 1) for line in (tmp_path / "env.txt").read_text().splitlines() if "=" in line)
    at = env["HTTPS_PROXY"]
    assert at.startswith("http://127.0.0.1:") and env["https_proxy"] == env["HTTP_PROXY"] == env["ALL_PROXY"] == at
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1"
    assert "long-password" not in str(env) and upstream.PROVIDER_ENV not in env
