"""A website as an ingest source: a polite, bounded crawl that stays on the host it started
on. A site that gives one page (an API reference drawn by a script, say) is asked for the
text it publishes for language models: llms-full.txt, else the pages llms.txt lists. Every request goes to an address checked beforehand (never this machine or the local
network unless the owner said so), and pages are saved into the job's staging folder."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from ..tools import resolve
from ..tools.base import ToolError
from . import parsers
from .archives import Found, Problem
from .limits import Budget

USER_AGENT = "HunterAI-kb/0.3"
MAX_PAGES, MAX_PAGES_LIMIT = 200, 5000
MAX_DEPTH, MAX_DEPTH_LIMIT = 2, 10
MAX_PAGE_BYTES = 5 * 1024**2
MAX_REDIRECTS = 5
MAX_URL_CHARS = 2000
MAX_QUEUE = 20_000           # links remembered; more than this are not followed
REQUEST_DEADLINE = 90.0      # seconds for one whole request
REQUESTS_PER_PAGE = 3        # the request limit is this many times max_pages, plus a few
CRAWL_DELAY = 0.5            # seconds between two requests
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TYPES = {"text/html": ".html", "application/xhtml+xml": ".html", "text/plain": ".txt", "text/markdown": ".md",
         "application/pdf": ".pdf", "application/json": ".json", "text/csv": ".csv", "application/xml": ".xml",
         "text/xml": ".xml", "application/rss+xml": ".xml", "application/atom+xml": ".xml"}
MAX_RENDERED = 200           # pages of one crawl opened in the browser
MIN_TEXT_CHARS = 200         # a page with less text than this, and a script, is tried in the browser
RENDER_TIMEOUT_MS = 30_000
MAX_FEED_LINKS = 2000
LLMS_FILES = ("llms-full.txt", "llms.txt")     # asked for in this order when a site gave one page
MIN_LLMS_BYTES = 200         # an answer shorter than this is an error page, not the file
_MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?[^)]*\)|<(https?://[^>\s]+)>|(https?://[^\s<>)\]]+)")
_sleep = asyncio.sleep


def feed_links(body: bytes, base: str) -> list[str] | None:
    """The post links of an RSS or Atom feed, in order; None when this is not a feed."""
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        return None
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    local = lambda el: el.tag.rsplit("}", 1)[-1].lower() if isinstance(el.tag, str) else ""  # noqa: E731
    if local(root) not in ("rss", "feed", "rdf"):
        return None
    out: list[str] = []
    for entry in root.iter():
        if local(entry) not in ("item", "entry"):
            continue
        href = None
        for el in entry:
            if local(el) != "link":
                continue
            if el.get("href"):
                if (el.get("rel") or "alternate") == "alternate":
                    href = el.get("href")
                    break
            elif (el.text or "").strip():
                href = el.text.strip()
                break
        if href and len(out) < MAX_FEED_LINKS:
            url = urldefrag(urljoin(base, href)).url
            if _usable(url) and url not in out:
                out.append(url)
    return out


def llms_links(text: str, base: str) -> list[str]:
    """The links of an llms.txt index (a Markdown list of pages), in order, no repeats."""
    out: dict[str, None] = {}
    for m in _MD_LINK.finditer(text):
        raw = m.group(1) or m.group(2) or m.group(3).rstrip(".,;:!?")
        try:
            url = urldefrag(urljoin(base, raw)).url
        except ValueError:
            continue
        if len(url) <= MAX_URL_CHARS and _usable(url) and len(out) < MAX_FEED_LINKS:
            out.setdefault(url)
    return list(out)


def _file_name(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1].lower()


def _related(a: str, b: str) -> bool:
    """Whether one host is the other or its parent site (docs.acme.test and acme.test)."""
    x, y = ((urlsplit(u).hostname or "").lower() for u in (a, b))
    short, long = sorted((x, y), key=len)
    return bool(short) and not resolve.is_ip(short) and (x == y or short.count(".") >= 1 and long.endswith("." + short))


def _text_chars(html: str) -> int:
    return len(parsers._html_text(html)[0])      # noqa: SLF001


def _needs_render(body: bytes) -> bool:
    """A page that is a shell for a script: next to no text of its own."""
    return b"<script" in body.lower() and _text_chars(body.decode("utf-8", "replace")) < MIN_TEXT_CHARS


class Renderer:
    """Opens a page in the browser the browser tools use, behind the same filtering proxy:
    the main document may only be on the crawled host, and nothing on this machine or the
    local network is loaded unless the owner said the site is there."""

    def __init__(self, allow_local: bool = False):
        self.allow_local = allow_local
        self._session = None

    async def page(self, url: str) -> str | None:
        from .. import browser
        if self._session is None:
            self._session = await browser._launch("kb-crawl")       # noqa: SLF001 - a session of its own, in no chat
        s = self._session
        if self.allow_local:
            await browser.allow(s, url)
        else:
            s.allowed.add(browser.host_of(url))
        try:
            await s.page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - a page that never goes quiet still has what it has drawn so far
            pass
        try:
            if await browser.off_site(s) or not s.page.url.startswith(("http://", "https://")):
                return None
            return await s.page.content()
        except Exception:  # noqa: BLE001
            return None

    async def close(self) -> None:
        s, self._session = self._session, None
        if s is None:
            return
        try:
            await asyncio.wait_for(s.cm.__aexit__(None, None, None), 15)
        except Exception:  # noqa: BLE001 - it is going away either way
            pass
        if s.proxy is not None:
            await s.proxy.stop()


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def links(html: str, base: str) -> list[str]:
    """Absolute http(s) links of a page, without fragments, in page order, no repeats."""
    p = _Links()
    try:
        p.feed(html)
    except Exception:   # noqa: BLE001 - broken markup gives the links found so far
        pass
    out: dict[str, None] = {}
    for href in p.hrefs:
        try:
            url = urldefrag(urljoin(base, href.strip())).url
        except ValueError:
            continue
        if len(url) <= MAX_URL_CHARS and _usable(url):
            out.setdefault(url)
    return list(out)


def _usable(url: str) -> bool:
    """A link the fetcher can take: http(s), a host, a sane port, nothing httpx refuses."""
    try:
        parts = urlsplit(url)
        parts.port                      # raises for a port that is not a number in range
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def validate(url: str) -> str:
    try:
        parts = urlsplit((url or "").strip())
        host = parts.hostname
    except ValueError:
        host, parts = None, None
    if not host or parts.scheme not in ("http", "https") or parts.username or parts.password \
            or len(url) > MAX_URL_CHARS or not _usable(parts.geturl()):
        raise ValueError("give an http or https URL without a user name or password")
    return urldefrag(parts.geturl()).url


def _same_site(a: str, b: str) -> bool:
    try:
        x, y = urlsplit(a), urlsplit(b)
        x.port, y.port
    except ValueError:
        return False
    return (x.hostname or "").lower() == (y.hostname or "").lower() and (
        x.port == y.port or {x.scheme, y.scheme} == {"http", "https"} and {x.port, y.port} <= {None, 80, 443})


class Fetcher:
    """Pinned-address GET requests, one site. `allow_local` is the owner saying the site is
    on this machine or network; without it such an address is refused, whoever names it."""

    def __init__(self, allow_local: bool = False):
        self.allow_local = allow_local
        self._ips: dict[str, str] = {}
        self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=TIMEOUT)

    async def close(self) -> None:
        await self._client.aclose()

    async def _ip(self, host: str) -> str:
        if host not in self._ips:
            ips = (host,) if resolve.is_ip(host) else await resolve.resolve_ips(host)
            if not ips:
                raise ToolError(f"{host} does not resolve")
            if not self.allow_local and any(resolve.is_local(ip) for ip in ips):
                raise ToolError(f"{host} is an address on this machine or local network")
            self._ips[host] = ips[0]
        return self._ips[host]

    async def get(self, url: str) -> tuple[int, dict[str, str], bytes, bool]:
        """(status, headers, body up to the cap, whether the body was cut). The body of a
        page whose type is not ingested is not downloaded."""
        try:
            return await asyncio.wait_for(self._get(url), REQUEST_DEADLINE)
        except asyncio.TimeoutError:
            raise ToolError("timed out") from None
        except (ValueError, httpx.InvalidURL) as e:
            raise ToolError(f"unusable URL: {type(e).__name__}") from None

    async def _get(self, url: str) -> tuple[int, dict[str, str], bytes, bool]:
        h = resolve.host_of_url(url)
        if resolve.is_own_api(h):
            raise ToolError("HunterAI's own address is not crawled")
        ip = await self._ip(h.host)
        scheme = urlsplit(url).scheme.lower()
        default = 443 if scheme == "https" else 80
        headers = {"Host": h.host if h.port == default else f"{h.host}:{h.port}", "User-Agent": USER_AGENT,
                   "Accept": "text/html,text/plain,application/pdf;q=0.9,*/*;q=0.5"}
        try:
            async with self._client.stream("GET", httpx.URL(url).copy_with(host=ip), headers=headers,
                                           extensions={"sni_hostname": h.host} if scheme == "https" else {}) as r:
                body = bytearray()
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if r.status_code != 200 or (ctype not in TYPES and not url.endswith("/robots.txt")):
                    return r.status_code, dict(r.headers), b"", False
                async for part in r.aiter_bytes():
                    body += part
                    if len(body) > MAX_PAGE_BYTES:
                        return r.status_code, dict(r.headers), bytes(body[:MAX_PAGE_BYTES]), True
                return r.status_code, dict(r.headers), bytes(body), False
        except httpx.TimeoutException:
            raise ToolError("timed out") from None
        except httpx.HTTPError as e:
            raise ToolError(f"request failed: {type(e).__name__}") from None


async def _robots(fetcher, base: str) -> robotparser.RobotFileParser:
    """The robots.txt rules of the site `base` is on (none when it has no such file)."""
    rules = robotparser.RobotFileParser()
    robots_url = urljoin(base, "/robots.txt")
    status, body = 0, b""
    for _ in range(MAX_REDIRECTS + 1):     # http to https is common; its rules still count
        status, headers, body, _ = await fetcher.get(robots_url)
        nxt = urljoin(robots_url, headers.get("location") or "") if 300 <= status < 400 else ""
        if not nxt or not _same_site(base, nxt):
            break
        robots_url = nxt
    rules.parse(body.decode("utf-8", "replace").splitlines() if status == 200 else [])
    return rules


async def crawl(source: dict, dest: Path, budget: Budget, fetcher: Fetcher | None = None,
                renderer=None) -> AsyncIterator[Found | Problem]:
    """Breadth-first from source["url"], within its host, to the page and depth limits. A feed
    is an index: its posts are queued at the feed's own depth and the feed is not kept. When
    the crawl gave one page, the site's llms-full.txt is taken, else the pages its llms.txt
    lists (not followed further); the page may name such a file on its parent site. With
    source["render"], a page that has next to no text without JavaScript is opened in the
    browser and kept as the browser drew it."""
    render = bool(source.get("render"))
    own_renderer = renderer is None
    rendered, render_note = 0, False
    start = validate(source["url"])
    max_pages = max(1, min(int(source.get("max_pages") or MAX_PAGES), MAX_PAGES_LIMIT))
    max_depth = max(0, min(int(source.get("max_depth") if source.get("max_depth") is not None else MAX_DEPTH), MAX_DEPTH_LIMIT))
    own = fetcher is None
    fetcher = fetcher or Fetcher(bool(source.get("allow_local")))
    dest.mkdir(parents=True, exist_ok=True)
    obey = bool(source.get("robots", True))
    rules: dict[str, robotparser.RobotFileParser] = {}      # by host: the start site, and a parent site taken up
    probes: set[str] = set()                                # llms files asked for unasked: a miss is not a problem
    named: list[str] = []                                   # llms files the pages link to on a related host
    host = lambda u: (urlsplit(u).hostname or "").lower()   # noqa: E731
    adopted: set[str] = set()                               # hosts of a parent site whose llms file is taken
    inside = lambda u: _same_site(start, u) or host(u) in adopted  # noqa: E731

    def allowed(u: str) -> bool:
        r = rules.get(host(u)) if obey else None
        return r is None or r.can_fetch(USER_AGENT, u)

    try:
        if obey:
            try:
                rules[host(start)] = await _robots(fetcher, start)
            except ToolError as e:
                if "local network" in str(e) or "own address" in str(e):
                    yield Problem(start, "failed", str(e))
                    return
        queue: list[tuple[str, int]] = [(start, 0)]
        seen = {start}
        saved = requests = 0
        max_requests = max_pages * REQUESTS_PER_PAGE + 10
        first, got_llms = True, False
        for llms in (None, *LLMS_FILES):
            if llms is not None:
                if saved > 1 or max_pages <= 1 or got_llms or queue:
                    break
                for link in [u for u in named if _file_name(u) == llms] + [urljoin(start, "/" + llms)]:
                    if link in seen:
                        continue
                    if not inside(link):                # on the parent site: under that site's own rules
                        try:
                            if obey and host(link) not in rules:
                                rules[host(link)] = await _robots(fetcher, link)
                        except ToolError:
                            continue
                        adopted.add(host(link))
                    seen.add(link)
                    probes.add(link)
                    queue.append((link, 0))
            while queue and saved < max_pages:
                if requests >= max_requests:
                    yield Problem(f"{start}#crawl", "skipped", f"request limit reached ({max_requests} requests): "
                                                                f"{len(queue)} links not visited")
                    return
                url, depth = queue.pop(0)
                probe = url in probes
                if not allowed(url):
                    if not probe:
                        yield Problem(url, "skipped", "disallowed by robots.txt")
                    continue
                if not first:
                    await _sleep(CRAWL_DELAY)
                first = False
                final, hops, result = url, 0, None
                while True:
                    requests += 1
                    try:
                        status, headers, body, cut = await fetcher.get(final)
                    except ToolError as e:
                        result = Problem(url, "failed", str(e)[:200])
                        break
                    location = headers.get("location")
                    if 300 <= status < 400 and location:
                        nxt = urldefrag(urljoin(final, location)).url
                        if not inside(nxt):
                            result = Problem(url, "skipped", f"redirect leaves the site ({urlsplit(nxt).hostname})")
                            break
                        hops += 1
                        if hops > MAX_REDIRECTS:
                            result = Problem(url, "failed", f"stopped after {MAX_REDIRECTS} redirects")
                            break
                        final = nxt
                        continue
                    break
                if result is not None:
                    if not probe:
                        yield result
                    continue
                if final != url:
                    if final in seen:
                        continue
                    seen.add(final)
                if status != 200:
                    if not probe:
                        yield Problem(final, "failed", f"HTTP {status}")
                    continue
                ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
                ext = TYPES.get(ctype)
                if probe and (ext not in (".txt", ".md") or cut or len(body) < MIN_LLMS_BYTES):
                    continue                    # the site has no such file: an error page or its app shell answered
                if ext is None:
                    yield Problem(final, "skipped", f"content type {ctype or 'unknown'} is not ingested")
                    continue
                if cut:
                    yield Problem(final, "skipped", f"page too large (over {MAX_PAGE_BYTES} bytes)")
                    continue
                if ext == ".xml":
                    posts = feed_links(body, final)
                    if posts is not None:
                        for link in posts:
                            if link not in seen and len(seen) < MAX_QUEUE and inside(link):
                                seen.add(link)
                                queue.append((link, depth))
                        continue
                if ext == ".html" and render and _needs_render(body):
                    if rendered >= MAX_RENDERED:
                        if not render_note:
                            render_note = True
                            yield Problem(f"{start}#render", "skipped", f"render limit reached ({MAX_RENDERED} pages): "
                                                                         "later pages are kept as fetched")
                    else:
                        rendered += 1
                        try:
                            if renderer is None:
                                renderer = Renderer(bool(source.get("allow_local")))
                            drawn = await renderer.page(final)
                        except ToolError as e:
                            render = False
                            yield Problem(f"{start}#render", "skipped", f"pages that need JavaScript were kept as fetched: {str(e)[:200]}")
                        else:
                            if drawn and _text_chars(drawn) > _text_chars(body.decode("utf-8", "replace")):
                                body = drawn.encode("utf-8", "replace")[:MAX_PAGE_BYTES]
                why = budget.refusal(len(body), stored=True)
                if why:
                    yield Problem(final, "skipped", why)
                    return
                saved += 1
                path = dest / f"page-{saved:05d}{ext}"
                path.write_bytes(body)
                budget.add(len(body), stored=True)
                yield Found(final, path, len(body))
                got_llms = got_llms or probe
                if ext in (".txt", ".md") and _file_name(final) == "llms.txt":
                    for link in llms_links(body.decode("utf-8", "replace"), final):     # an index: its pages, no further
                        if link not in seen and len(seen) < MAX_QUEUE and inside(link):
                            seen.add(link)
                            queue.append((link, max_depth))
                if ext == ".html":
                    for link in links(body.decode("utf-8", "replace"), final):
                        if _file_name(link) in LLMS_FILES and not inside(link) and _related(start, link) and link not in named:
                            named.append(link)
                        if depth < max_depth and link not in seen and len(seen) < MAX_QUEUE and inside(link):
                            seen.add(link)
                            queue.append((link, depth + 1))
        if queue and saved >= max_pages:
            yield Problem(f"{start}#crawl", "skipped", f"page limit reached ({max_pages} pages): {len(queue)} links not visited")
    finally:
        if own:
            await fetcher.close()
        if own_renderer and renderer is not None:
            await renderer.close()
