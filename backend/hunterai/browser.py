"""Browser sessions for the browser tools: one Camoufox (Firefox) window per chat, headless,
closed after a while without use and when the app stops.

What a page may load is policed here, not left to the page, in two layers. Every byte the
browser sends goes through a filtering proxy (browser_proxy.py) that resolves the name
itself, refuses this machine and the local network unless the owner allowed that exact host
and port, and connects to the address it checked: redirects, WebSockets, and workers
included. On top of that the main document may only be on hosts the owner allowed for this
session (through browser_navigate's permission step); a tab that ends up elsewhere is sent
back to a blank page and the model is told."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import browser_proxy
from .tools import resolve
from .tools.base import ToolError

IDLE_SECONDS = 900
MAX_LOG = 300
NAV_TIMEOUT_MS = 45_000
ACTION_TIMEOUT_MS = 15_000
MAX_SESSIONS = 3
# The proxy is the only way out, for local names too; no second resolver, no workers that
# outlive a page, no speculative connections made before a request is checked.
PREFS = {"network.proxy.allow_hijacking_localhost": True, "network.proxy.no_proxies_on": "",
         "network.proxy.socks_remote_dns": True, "network.dns.disablePrefetch": True, "network.prefetch-next": False,
         "network.predictor.enabled": False, "network.http.speculative-parallel-limit": 0,
         # (Switching service workers off would also switch off the request hook below: a
         # worker's requests go through the proxy like every other, so they stay on.)
         "network.trr.mode": 5, "media.peerconnection.enabled": False,
         "network.captive-portal-service.enabled": False, "network.connectivity-service.enabled": False}


@dataclass
class Session:
    chat_id: str
    cm: object
    browser: object
    page: object
    proxy: browser_proxy.Proxy | None = None
    allowed: set[str] = field(default_factory=set)       # hosts the main document may be on
    local_ok: set[tuple[str, int]] = field(default_factory=set)   # allowed (host, port) that are local addresses
    network: list[dict] = field(default_factory=list)
    console: list[dict] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    used: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _ips: dict[str, bool] = field(default_factory=dict)
    via_caido: bool = False                               # its traffic goes through Caido and is recorded there


_sessions: dict[str, Session] = {}
_reaper: asyncio.Task | None = None


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


async def _is_local(s: Session, host: str) -> bool:
    """Whether this host is (or resolves to) this machine or the local network. Cached per session."""
    if host not in s._ips:
        try:
            if resolve.is_ip(host):
                s._ips[host] = resolve.is_local(host)
            elif host == "localhost" or host.endswith(".localhost"):
                s._ips[host] = True
            else:
                s._ips[host] = any(resolve.is_local(ip) for ip in await resolve.resolve_ips(host))
        except (ToolError, OSError):
            s._ips[host] = True          # what cannot be resolved is not let through
    return s._ips[host]


async def decide_request(s: Session, url: str, *, is_document: bool, is_main_frame: bool) -> str | None:
    """None to let a request go, else why it is blocked. The page never decides this."""
    parts = urlsplit(url)
    if parts.scheme in ("data", "blob", "about"):
        return None
    if parts.scheme not in ("http", "https", "ws", "wss"):
        return f"scheme {parts.scheme or '(none)'} is not loaded"
    host = (parts.hostname or "").lower()
    if not host:
        return "no host"
    try:
        if resolve.is_own_api(resolve.host_of_url(url if parts.scheme in ("http", "https") else "http://" + parts.netloc)):
            return "HunterAI's own address"
    except ToolError:
        return "unusable address"
    if is_document and is_main_frame and host not in s.allowed:
        return f"navigation to {host} was not allowed for this session"
    port = parts.port or (443 if parts.scheme in ("https", "wss") else 80)
    if (host, port) not in s.local_ok and await _is_local(s, host):
        return f"{host} is an address on this machine or local network"
    return None


def _log(buffer: list[dict], entry: dict) -> None:
    buffer.append(entry)
    del buffer[:-MAX_LOG]


async def _launch(chat_id: str) -> Session:
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError:
        raise ToolError("the browser is not installed (the camoufox package is missing)") from None
    proxy = browser_proxy.Proxy()
    via = await _caido_proxy()            # before anything is started: a Caido that cannot be reached is an error here
    proxy.via = via
    port = await proxy.start()
    cm = None
    try:
        cm = AsyncCamoufox(headless=True, proxy={"server": f"http://127.0.0.1:{port}"}, block_webrtc=True,
                           i_know_what_im_doing=True, firefox_user_prefs=PREFS)
        browser = await cm.__aenter__()
        # Caido reads https by answering with its own certificates, which the browser has to accept.
        page = await browser.new_page(ignore_https_errors=True) if via else await browser.new_page()
    except Exception as e:  # noqa: BLE001 - a missing browser binary, a display problem, anything
        await proxy.stop()
        if cm is not None:
            try:
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        raise ToolError(f"the browser could not start: {type(e).__name__}: {str(e)[:200]}. "
                        "Run `camoufox fetch` once if its browser was never downloaded.") from None
    s = Session(chat_id, cm, browser, page, proxy=proxy, via_caido=via is not None)
    proxy.local_ok = s.local_ok                       # one set: what allow() adds, the proxy honours
    proxy.on_block = lambda why: _log(s.console, {"type": "blocked", "text": why})
    page.set_default_timeout(ACTION_TIMEOUT_MS)

    async def no_popup(new_page) -> None:
        # A second tab would be out of the tools' sight: it is closed, and that is reported.
        _log(s.blocked, "a new tab or window the page tried to open (closed)")
        try:
            await new_page.close()
        except Exception:  # noqa: BLE001
            pass
    page.context.on("page", lambda p: asyncio.ensure_future(no_popup(p)) if p is not page else None)

    async def route(r) -> None:
        req = r.request
        try:
            main = req.frame == page.main_frame
        except Exception:  # noqa: BLE001
            main = False
        try:
            why = await decide_request(s, req.url, is_document=req.resource_type == "document", is_main_frame=main)
        except Exception as e:  # noqa: BLE001 - a check that cannot be made is a refusal
            why = f"the request could not be checked ({type(e).__name__}: {str(e)[:120]})"
            _log(s.console, {"type": "blocked", "text": why})
        if why is None:
            await r.continue_()
        else:
            if req.resource_type == "document":
                _log(s.blocked, f"{req.url[:300]} ({why})")
            await r.abort()
    await page.context.route("**/*", route)
    page.on("response", lambda resp: _log(s.network, {"method": resp.request.method, "url": resp.url[:500], "status": resp.status,
                                                     "type": resp.request.resource_type}))
    page.on("requestfailed", lambda req: _log(s.network, {"method": req.method, "url": req.url[:500], "status": "failed",
                                                         "type": req.resource_type}))
    page.on("console", lambda m: _log(s.console, {"type": m.type, "text": (m.text or "")[:1000]}))
    page.on("pageerror", lambda e: _log(s.console, {"type": "pageerror", "text": str(e)[:1000]}))
    return s


async def _caido_proxy() -> tuple[str, int] | None:
    """Where Caido's proxy is, when the owner switched on "browser through Caido"; else None."""
    from . import caido, db
    from .repo import settings
    conn = await db.connect()
    try:
        on = bool(await settings.get(conn, "browser_via_caido", False))
    finally:
        await conn.close()
    if not on:
        return None
    try:
        return await caido.proxy_address()
    except caido.CaidoError as e:
        raise ToolError(f"the browser is set to go through Caido, and Caido is not usable: {e}. "
                        'Switch "Send the browser and web_fetch through Caido" off in Settings to browse without it.') from None


def sessions() -> list[Session]:
    return list(_sessions.values())


async def get(chat_id: str, *, create: bool = False) -> Session:
    """The chat's session. ToolError when there is none and `create` is off."""
    global _reaper
    s = _sessions.get(chat_id)
    if s is None:
        if not create:
            raise ToolError("no page is open in this chat's browser; open one with browser_navigate first")
        if len(_sessions) >= MAX_SESSIONS:
            idle = [x for x in _sessions.values() if not x.lock.locked()]
            if not idle:
                raise ToolError(f"{MAX_SESSIONS} browser sessions are busy in other chats; try again when one is done")
            await close(min(idle, key=lambda x: x.used).chat_id)
        s = _sessions[chat_id] = await _launch(chat_id)
        if _reaper is None or _reaper.done():
            _reaper = asyncio.create_task(_reap())
    s.used = time.monotonic()
    return s


async def allow(s: Session, url: str) -> None:
    """The owner allowed this site (browser_navigate passed its permission step). A local site
    is allowed on that one port only."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    s.allowed.add(host)
    if await _is_local(s, host):
        s.local_ok.add((host, parts.port or (443 if parts.scheme == "https" else 80)))


async def off_site(s: Session) -> str | None:
    """When the tab has ended up on a host that was not allowed (a redirect the first request
    did not show): leave it, and say where it had gone."""
    url = s.page.url
    host = host_of(url)
    if not url.startswith(("http://", "https://")) or host in s.allowed:
        return None
    try:
        await s.page.goto("about:blank")
    except Exception:  # noqa: BLE001
        pass
    return f"the site sent the browser on to {host}, which was not allowed for this session; the page was left"


async def close(chat_id: str) -> bool:
    s = _sessions.pop(chat_id, None)
    if s is None:
        return False
    try:
        if s.cm is not None:
            await asyncio.wait_for(s.cm.__aexit__(None, None, None), 15)
    except Exception:  # noqa: BLE001 - it is going away either way
        pass
    if s.proxy is not None:
        await s.proxy.stop()
    return True


async def shutdown() -> None:
    global _reaper
    if _reaper is not None:
        _reaper.cancel()
        _reaper = None
    for chat_id in list(_sessions):
        await close(chat_id)


async def _reap() -> None:
    while _sessions:
        await asyncio.sleep(30)
        for chat_id, s in list(_sessions.items()):
            if time.monotonic() - s.used > IDLE_SECONDS and not s.lock.locked():
                await close(chat_id)


def open_chats() -> list[str]:
    return list(_sessions)
