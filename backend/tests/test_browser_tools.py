import asyncio

import pytest

from hunterai import browser
from hunterai.providers.base import ToolCall
from hunterai.tools import browser_tools, registry
from hunterai.tools.base import ToolContext, ToolError
from tests.toolkit import answer_next, run_call, seeded_run


class FakeLocator:
    def __init__(self, page, key):
        self.page, self.key, self.first = page, key, self

    async def click(self):
        self.page.actions.append(("click", self.key))
        if self.key in self.page.links:
            self.page.url = self.page.links[self.key]

    async def fill(self, value):
        self.page.actions.append(("fill", self.key, value))

    async def press(self, key):
        self.page.actions.append(("press", self.key, key))

    async def count(self):
        return 2

    def nth(self, i):
        return self

    async def inner_text(self):
        return f"text of {self.key}"

    async def inner_html(self):
        return f"<b>{self.key}</b>"


class FakeResponse:
    status = 200


class FakePage:
    def __init__(self):
        self.url, self.actions, self.links, self.body = "about:blank", [], {}, "Welcome to acme"
        self.fail = None

    async def goto(self, url, **kw):
        if self.fail:
            raise self.fail
        self.url = url
        return FakeResponse()

    async def title(self):
        return "Acme"

    async def inner_text(self, selector):
        return self.body

    def locator(self, sel):
        return FakeLocator(self, sel)

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, f"text={text}")

    async def wait_for_load_state(self, *a, **kw):
        return None

    async def screenshot(self, path, full_page=False):
        with open(path, "wb") as f:
            f.write(b"\x89PNG fake")


@pytest.fixture
def session(monkeypatch):
    """Sessions are fake pages; nothing launches a browser."""
    made = {}

    async def launch(chat_id):
        made[chat_id] = browser.Session(chat_id, None, None, FakePage())
        return made[chat_id]
    monkeypatch.setattr(browser, "_launch", launch)
    monkeypatch.setattr(browser, "_sessions", {})

    async def no_close(chat_id):
        return browser._sessions.pop(chat_id, None) is not None
    monkeypatch.setattr(browser, "close", no_close)
    return made


def ctx_for(chat, tmp_path):
    return ToolContext(chat_id=chat.id, run_id="r", step_id="s", call_id="c", mode="pentesting", target=None, workdir=tmp_path, cancel=asyncio.Event())


async def test_request_policy_main_document_only_to_allowed_hosts_and_never_local(dns):
    table, _ = dns
    table.update({"acme.test": ["93.184.216.34"], "cdn.test": ["93.184.216.35"], "evil.test": ["93.184.216.36"],
                  "rebind.test": [["93.184.216.37", "127.0.0.1"]], "intranet.test": ["10.0.0.5"]})
    s = browser.Session("c", None, None, FakePage())
    await browser.allow(s, "https://acme.test/")
    ok = lambda url, doc=False, main=True: browser.decide_request(s, url, is_document=doc, is_main_frame=main)  # noqa: E731
    assert await ok("https://acme.test/login", True) is None
    assert await ok("https://cdn.test/app.js") is None                                    # a script from elsewhere loads
    assert "was not allowed" in await ok("https://evil.test/", True)                      # the page cannot take the tab elsewhere
    assert await ok("https://evil.test/frame", True, main=False) is None                   # an embedded frame is not a navigation
    for url in ("http://127.0.0.1:8790/api/settings", "http://localhost:8790/api/mcp/servers"):
        assert await ok(url) == "HunterAI's own address"
    for url in ("http://127.0.0.1:9000/x", "http://intranet.test/x", "http://rebind.test/x", "http://[::1]/x", "http://169.254.169.254/latest", "http://unknown.test/"):
        assert "this machine or local network" in await ok(url), url
    assert "is not loaded" in await ok("file:///etc/passwd") and await ok("data:text/plain,x") is None
    await browser.allow(s, "http://intranet.test/")                                       # the owner allowed a local site
    assert await ok("http://intranet.test/admin", True) is None and "local network" in await ok("http://127.0.0.1:9000/x")
    assert "local network" in await ok("http://intranet.test:6333/collections")           # that port only


async def test_navigate_asks_for_the_host_and_returns_the_page_as_untrusted_text(conn, session, dns):
    dns[0]["acme.test"] = ["93.184.216.34"]
    for t in browser_tools.tools():
        registry.register(t)
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c1", "browser_navigate", {"url": "https://acme.test/login"})))
    req = await answer_next(run_id, "allow_chat")
    out, _, _ = await task
    assert (req.risk, req.hosts[0]["host"], req.grant_target) == ("network", "acme.test", "browser https://acme.test:443")
    assert "clicking, and typing" in req.preview
    text = out.block.content
    assert not out.block.is_error and "Status: 200" in text and "URL: https://acme.test/login" in text and "untrusted data" in text and "Welcome to acme" in text
    assert session[chat.id].allowed == {"acme.test"}
    # a click on the same site is covered by the same "for this chat" answer; it names that site
    session[chat.id].page.links = {"#go": "https://acme.test/next"}
    out, events, _ = await run_call(conn, chat, run_id, ToolCall("c2", "browser_click", {"selector": "#go"}))
    assert "URL: https://acme.test/next" in out.block.content and all(e["type"] != "permission_request" for e in events)
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c3", "browser_type", {"text": "Search", "value": "idor", "submit": True}))
    assert ("fill", "text=Search", "idor") in session[chat.id].page.actions and ("press", "text=Search", "Enter") in session[chat.id].page.actions
    # another host asks again
    other = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c4", "browser_navigate", {"url": "https://other.test/"})))
    req = await answer_next(run_id, "deny")
    out, _, _ = await other
    assert req.hosts[0]["host"] == "other.test" and out.block.is_error and session[chat.id].allowed == {"acme.test"}


async def test_reading_tools_need_an_open_page_and_report_logs(conn, session, tmp_path):
    chat, _ = await seeded_run(conn)
    ctx = ctx_for(chat, tmp_path)
    tools = {t.name: t for t in browser_tools.tools()}
    assert {n: t.risk for n, t in tools.items()} == {"browser_navigate": "network", "browser_click": "network", "browser_type": "network",
        "browser_read": "read", "browser_screenshot": "read", "browser_network": "read", "browser_console": "read", "browser_close": "read"}
    with pytest.raises(ToolError, match="open one with browser_navigate"):
        await tools["browser_read"].handler(ctx, {})
    s = await browser.get(chat.id, create=True)
    s.page.url = "https://acme.test/"
    s.network += [{"method": "GET", "url": "https://acme.test/api/me", "status": 200, "type": "xhr"}, {"method": "GET", "url": "https://cdn.test/a.js", "status": 200, "type": "script"}]
    s.console.append({"type": "error", "text": "TypeError: x is undefined"}); s.blocked.append("https://evil.test/ (navigation to evil.test was not allowed for this session)")
    assert "200 GET xhr https://acme.test/api/me" in (await tools["browser_network"].handler(ctx, {"contains": "api"})).text
    assert "cdn.test" not in (await tools["browser_network"].handler(ctx, {"contains": "api"})).text
    assert "[error] TypeError" in (await tools["browser_console"].handler(ctx, {})).text
    page = (await tools["browser_read"].handler(ctx, {})).text
    assert "Blocked: https://evil.test/" in page and s.blocked == []
    assert page.index("untrusted data") < page.index("URL:") < page.index("Title:")
    part = (await tools["browser_read"].handler(ctx, {"selector": ".row", "html": True})).text
    assert "2 element(s) match" in part and "<b>.row</b>" in part
    shot = (await tools["browser_screenshot"].handler(ctx, {})).text
    saved = list((tmp_path / "screenshots").glob("page-*.png"))
    assert len(saved) == 1 and str(saved[0]) in shot
    with pytest.raises(ToolError, match="selector or the visible text"):
        await tools["browser_click"].handler(ctx, {})
    assert (await tools["browser_close"].handler(ctx, {})).text == "The browser was closed." and (await tools["browser_close"].handler(ctx, {})).text == "No browser was open."


async def test_a_page_that_fails_to_load_is_an_error_with_the_reason(conn, session, tmp_path, dns):
    dns[0]["x.test"] = ["93.184.216.34"]
    chat, _ = await seeded_run(conn)
    ctx = ctx_for(chat, tmp_path)
    nav = next(t for t in browser_tools.tools() if t.name == "browser_navigate")
    s = await browser.get(chat.id, create=True)
    s.page.fail = RuntimeError("NS_ERROR_UNKNOWN_HOST\nmore"); s.blocked.append("https://x.test/ (x.test is an address on this machine or local network)")
    with pytest.raises(ToolError, match="did not load: https://x.test/ \\(x.test is an address"):
        await nav.handler(ctx, {"url": "https://x.test/"})
    with pytest.raises(ToolError, match="only http and https"):
        await nav.prepare(ctx, {"url": "file:///etc/passwd"})


async def test_sessions_are_capped_and_idle_ones_close(session, monkeypatch):
    monkeypatch.setattr(browser, "MAX_SESSIONS", 2)
    a = await browser.get("a", create=True); a.used -= 100
    await browser.get("b", create=True)
    await browser.get("c", create=True)
    assert set(browser.open_chats()) == {"b", "c"}                                       # the least recently used one made room
    async with browser._sessions["b"].lock, browser._sessions["c"].lock:
        with pytest.raises(ToolError, match="busy in other chats"):                      # a session in the middle of a call is never closed
            await browser.get("d", create=True)
    await browser.shutdown()
    assert browser.open_chats() == []


async def test_a_tab_that_ends_up_on_another_site_is_left_and_reported(conn, session, tmp_path, dns):
    dns[0]["acme.test"] = ["93.184.216.34"]
    chat, _ = await seeded_run(conn)
    ctx = ctx_for(chat, tmp_path)
    tools = {t.name: t for t in browser_tools.tools()}
    s = await browser.get(chat.id, create=True)
    real = s.page.goto

    async def redirecting(url, **kw):                       # the site answers with a redirect the first request did not show
        r = await real(url, **kw)
        if url != "about:blank":
            s.page.url = "https://evil.test/landing"
        return r
    s.page.goto = redirecting
    out = await tools["browser_navigate"].handler(ctx, {"url": "https://acme.test/"})
    assert out.is_error and "sent the browser on to evil.test" in out.text and s.page.url == "about:blank"
