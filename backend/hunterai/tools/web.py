"""Web tools. Every request goes to the address that was resolved and checked by the
permission engine (no second DNS lookup), and a redirect to another host is returned to
the model instead of followed, so a new host always gets its own permission decision."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .. import procs
from . import resolve
from .base import HostUse, PathUse, Prepared, Tool, ToolContext, ToolError, ToolResult

MAX_REDIRECTS = 5
MAX_FETCH_BYTES = 2 * 1024**2
MAX_BODY_BYTES = 1024**2
TIMEOUT = httpx.Timeout(60.0, connect=10.0)     # per read
FETCH_DEADLINE = 120                            # seconds, for a whole call
DOWNLOAD_DEADLINE = 1800
DEFAULT_DOWNLOAD_MB = 200
MAX_DOWNLOAD_MB = 2048
FREE_DISK_FLOOR = procs.FREE_DISK_FLOOR
USER_AGENT = "HunterAI/0.2"
FORBIDDEN_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}
_MB = 1024**2


# ---------------------------------------------------------------- html

_DROP = {"script", "style", "noscript", "template", "svg"}
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
          "section", "article", "header", "footer", "nav", "pre", "blockquote", "hr", "form",
          "dt", "dd", "title", "body", "main", "aside", "td", "th"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title: list[str] = []
        self.skip = 0
        self.in_head = self.in_title = False
        self.links: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self.skip += 1
        elif tag == "head":
            self.in_head = True
        elif tag == "title":
            self.in_title = True
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append(href if href.startswith(("http://", "https://")) else None)
        if tag in _BLOCK and not self.in_head:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP:
            self.skip = max(0, self.skip - 1)
        elif tag == "head":
            self.in_head = False
        elif tag == "title":
            self.in_title = False
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href and not self.skip and not self.in_head:
                self.out.append(f" [{href}]")
        if tag in _BLOCK and not self.in_head:
            self.out.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title.append(data)
        elif not self.in_head:
            self.out.append(data)


def html_to_text(html: str) -> str:
    """Readable text of a page: scripts and styles dropped, the title first, links kept."""
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:   # a broken page still gives what was parsed so far
        pass
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in "".join(p.out).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    title = re.sub(r"\s+", " ", "".join(p.title)).strip()
    return f"{title}\n\n{text}".strip() if title else text


# ---------------------------------------------------------------- pinned requests

def _origin(url: str) -> tuple[str, HostUse]:
    return urlsplit(url).scheme.lower(), resolve.host_of_url(url)


def _pinned(method: str, url: str, host: HostUse, ip: str, headers: dict, body: bytes | None):
    """The request for `url`, sent to `ip`: Host and SNI carry the real name."""
    scheme = urlsplit(url).scheme.lower()
    default = 443 if scheme == "https" else 80
    send = dict(headers)
    send["Host"] = host.host if host.port == default else f"{host.host}:{host.port}"
    send.setdefault("User-Agent", USER_AGENT)
    return {"method": method, "url": httpx.URL(url).copy_with(host=ip), "headers": send,
            "content": body,
            "extensions": {"sni_hostname": host.host} if scheme == "https" else {}}


def _redirect_target(ctx: ToolContext, current: str, location: str, ips: tuple[str, ...]):
    """(new url, None) when the redirect may be followed, else (new url, what it leaves)."""
    new = urljoin(current, location)
    scheme, cur = _origin(current)
    try:
        new_scheme, nxt = _origin(new)
        path = resolve.url_path(new)
    except ToolError:
        return new, cur.host
    same_port = nxt.port == cur.port or (scheme == "http" and new_scheme == "https"
                                         and (cur.port, nxt.port) == (80, 443))
    if nxt.host != cur.host or not same_port:
        return new, cur.host
    verdict = resolve.scope_verdict(ctx.target, (HostUse(nxt.host, nxt.port, ips),), path)
    if verdict.out or verdict.not_in:
        return new, "this target's scope"
    return new, None


class _NotFollowed(Exception):
    def __init__(self, status: int, url: str, leaves: str):
        self.text = (f"redirect ({status}) to {url} was not followed: it leaves {leaves}. "
                     "Call the tool on that URL if you need it.")


async def _via_caido(ctx: ToolContext | None) -> tuple[tuple[str, int] | None, str]:
    """(Caido's proxy, "") when the owner switched on recording in Caido and it can be used;
    (None, why it is not recorded) when it is switched on and Caido cannot be used; else (None, "")."""
    from .. import caido, db
    from ..repo import settings
    if not caido.available():
        return None, ""
    conn = await db.connect()
    try:
        on = bool(await settings.get(conn, "browser_via_caido", False))
    finally:
        await conn.close()
    if not on:
        return None, ""
    try:
        return await caido.proxy_address(), ""
    except caido.CaidoError as e:
        return None, f"not recorded in Caido: {e}"


def _named(method: str, url: str, headers: dict, body: bytes | None):
    """The request for `url` as a proxy is asked for it: by name. Caido's way out is HunterAI's
    filter, which resolves the name, checks the address, and connects to the one it checked."""
    send = dict(headers)
    send.setdefault("User-Agent", USER_AGENT)
    return {"method": method, "url": url, "headers": send, "content": body}


async def _open(stack, client, ctx: ToolContext, method: str, url: str, headers: dict,
                body: bytes | None, via: tuple[str, int] | None = None):
    """Send the request, following allowed redirects on the pinned address (or, with `via`,
    through that proxy by name). Returns (response, final url, redirects followed). The
    response is still streaming."""
    ips = ctx.prepared.hosts[0].ips
    if not ips:
        raise ToolError("the host was not resolved")
    followed: list[str] = []
    while True:
        _, host = _origin(url)
        try:
            response = await stack.enter_async_context(client.stream(
                **(_named(method, url, headers, body) if via else _pinned(method, url, host, ips[0], headers, body))))
        except httpx.TimeoutException:
            raise ToolError(f"request to {host.host} timed out") from None
        except httpx.HTTPError as e:
            raise ToolError(f"request to {host.host} failed: {type(e).__name__}") from None
        location = response.headers.get("location")
        if not (300 <= response.status_code < 400 and location):
            return response, url, followed
        new, leaves = _redirect_target(ctx, url, location, ips)
        if leaves is not None:
            raise _NotFollowed(response.status_code, new, leaves)
        if len(followed) >= MAX_REDIRECTS:
            raise ToolError(f"stopped after {MAX_REDIRECTS} redirects (last: {new})")
        if response.status_code in (301, 302, 303) and method == "POST":
            method, body = "GET", None
            headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        await response.aclose()
        followed.append(f"{response.status_code} -> {new}")
        url = new


def _client(via: tuple[str, int] | None = None) -> httpx.AsyncClient:
    if via is None:
        return httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=TIMEOUT)
    # Caido reads https by answering with its own certificates: they are accepted from it, on loopback, only.
    return httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=TIMEOUT,
                             proxy=f"http://{via[0]}:{via[1]}", verify=False)


# ---------------------------------------------------------------- web_fetch

_TEXTUAL = ("text/", "application/json", "application/xml", "application/xhtml+xml",
            "application/javascript", "application/x-javascript", "application/ld+json",
            "application/x-www-form-urlencoded", "application/problem+json")


async def _prepare_fetch(ctx: ToolContext, args: dict) -> Prepared:
    method = args.get("method") or "GET"
    scheme, h = _origin(args["url"])
    for name in args.get("headers") or {}:
        if name.lower() in FORBIDDEN_HEADERS:
            raise ToolError(f"the {name} header is set by HunterAI and cannot be given")
    body = args.get("body")
    if body is not None and method == "GET":
        raise ToolError("a body needs method POST")
    if body is not None and len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ToolError(f"the body is larger than {MAX_BODY_BYTES // _MB} MiB")
    return Prepared(hosts=(h,), url_path=resolve.url_path(args["url"]), resolve_hosts=True,
                    grant_target=f"{method} {scheme}://{h.host}:{h.port}")


async def _fetch(ctx: ToolContext, args: dict) -> ToolResult:
    from contextlib import AsyncExitStack
    method = args.get("method") or "GET"
    body = args["body"].encode("utf-8") if args.get("body") is not None else None
    via, note = await _via_caido(ctx)
    local, allowed = None, None
    if via is not None:
        from .. import caido
        checked = ctx.prepared.hosts[0]
        if any(resolve.is_local(ip) for ip in checked.ips):
            local = (checked.host, checked.port)      # the owner allowed this local address for this call
        allowed = (await caido.egress()).local_ok.own
    try:
        async with asyncio.timeout(FETCH_DEADLINE), AsyncExitStack() as stack:
            client = await stack.enter_async_context(_client(via))
            if local:
                allowed.add(local)
                stack.callback(allowed.discard, local)
            try:
                response, final, followed = await _open(stack, client, ctx, method, args["url"],
                                                        dict(args.get("headers") or {}), body, via)
            except _NotFollowed as e:
                return ToolResult(e.text)
            ctype = response.headers.get("content-type", "")
            kind = ctype.split(";")[0].strip().lower()
            data, cut = b"", False
            try:
                async for chunk in response.aiter_bytes():
                    data += chunk
                    if len(data) >= MAX_FETCH_BYTES:
                        data, cut = data[:MAX_FETCH_BYTES], True
                        break
            except httpx.HTTPError as e:
                raise ToolError(f"reading from {_origin(final)[1].host} failed: "
                                f"{type(e).__name__}") from None
            status, headers = response.status_code, response.headers
    except TimeoutError:
        raise ToolError(f"web_fetch stopped: it took longer than {FETCH_DEADLINE} s") from None
    if kind and not kind.startswith(_TEXTUAL) and not kind.endswith(("+json", "+xml")):
        text = f"[binary body, {len(data)} bytes, {ctype}; use web_download]"
    else:
        m = re.search(r"charset=([\w.-]+)", ctype, re.I)
        try:
            text = data.decode(m.group(1) if m else "utf-8", errors="replace")
        except LookupError:
            text = data.decode("utf-8", errors="replace")
        if kind in ("text/html", "application/xhtml+xml") and not args.get("raw"):
            text = html_to_text(text)
        if cut:
            text += f"\n[body cut at {MAX_FETCH_BYTES // _MB} MiB]"
    lines = [f"HTTP {status} {final}", f"content-type: {ctype}"]
    if args.get("raw"):
        lines += [f"{k}: {v}" for k, v in headers.items() if k.lower() != "content-type"]
    if followed:
        lines.append("redirects: " + "; ".join(followed))
    if via is not None:
        lines.append("recorded in Caido: the caido tool lists this request and can show it in full")
    elif note:
        lines.append(note)
    return ToolResult("\n".join(lines) + "\n\n" + text)


# ---------------------------------------------------------------- web_download

def _safe_name(args: dict) -> str:
    raw = args.get("filename") or urlsplit(args["url"]).path.rsplit("/", 1)[-1] or "download"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(raw.replace("\\", "/"))).lstrip(".")
    return (name or "download")[:120]


async def _prepare_download(ctx: ToolContext, args: dict) -> Prepared:
    scheme, h = _origin(args["url"])
    folder = ctx.workdir / "downloads"
    name = _safe_name(args)
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    n, candidate = 0, name
    while (folder / candidate).exists() or (folder / (candidate + ".part")).exists():
        n += 1
        candidate = f"{stem}-{n}" + (f".{ext}" if ext else "")
    dest = resolve.resolve_path(str(folder / candidate), ctx.workdir)
    if not resolve.is_inside(dest, ctx.workdir):
        raise ToolError("the download folder resolves outside the working folder")
    return Prepared(paths=(PathUse(str(dest), "write"),), hosts=(h,),
                    url_path=resolve.url_path(args["url"]), resolve_hosts=True,
                    grant_target=f"GET {scheme}://{h.host}:{h.port}")


def _free(folder: Path) -> int:
    p = folder
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def _low_disk(free: int) -> ToolError:
    return ToolError(f"not enough free disk: {free / 1024**3:.1f} GiB free, the floor is 10 GiB")


async def _download(ctx: ToolContext, args: dict) -> ToolResult:
    from contextlib import AsyncExitStack
    dest = Path(ctx.prepared.paths[0].path)
    cap_mb = int(args.get("max_mb") or DEFAULT_DOWNLOAD_MB)
    too_big = ToolError(f"download stopped: larger than the limit of {cap_mb} MB")
    if _free(dest.parent) < FREE_DISK_FLOOR:
        raise _low_disk(_free(dest.parent))
    part = Path(str(dest) + ".part")
    made = False
    try:
        async with asyncio.timeout(DOWNLOAD_DEADLINE), AsyncExitStack() as stack:
            client = await stack.enter_async_context(_client())
            try:
                response, final, _ = await _open(stack, client, ctx, "GET", args["url"], {}, None)
            except _NotFollowed as e:
                return ToolResult(e.text)
            if response.status_code >= 400:
                raise ToolError(f"download failed: HTTP {response.status_code} for {final}")
            length = int(response.headers.get("content-length") or 0)
            if length > cap_mb * _MB:
                raise too_big
            free = _free(dest.parent)
            if free - length < FREE_DISK_FLOOR:
                raise _low_disk(free)
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            made = True
            digest, size, since_check = hashlib.sha256(), 0, 0
            with os.fdopen(fd, "wb") as f:
                try:
                    async for chunk in response.aiter_bytes():
                        if ctx.cancel.is_set():
                            raise asyncio.CancelledError
                        size += len(chunk)
                        if size > cap_mb * _MB:
                            raise too_big
                        f.write(chunk)
                        digest.update(chunk)
                        since_check += len(chunk)
                        if since_check >= 8 * _MB:
                            since_check = 0
                            if _free(dest.parent) < FREE_DISK_FLOOR:
                                raise _low_disk(_free(dest.parent))
                except httpx.HTTPError as e:
                    raise ToolError(f"download failed: {type(e).__name__}") from None
            ctype = response.headers.get("content-type", "unknown")
        if resolve.resolve_path(str(dest), ctx.workdir) != dest:
            raise ToolError("the download folder changed during the download")
        os.replace(part, dest)
        made = False
        return ToolResult(f"saved {dest} ({size} bytes, sha256 {digest.hexdigest()}, "
                          f"content-type {ctype})")
    except TimeoutError:
        raise ToolError(f"web_download stopped: it took longer than {DOWNLOAD_DEADLINE} s") from None
    finally:
        if made:
            try:
                os.unlink(part)
            except OSError:
                pass


def tools() -> list[Tool]:
    return [
        Tool("web_fetch", (
            "Fetch a URL (GET or POST). HTML is reduced to text unless raw=true. A redirect to "
            "another host is reported, not followed: call the tool on the new URL."),
            {"type": "object", "properties": {
                "url": {"type": "string"}, "method": {"enum": ["GET", "POST"]},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {"type": "string"}, "raw": {"type": "boolean"}},
             "required": ["url"], "additionalProperties": False},
            "network", _fetch, prepare=_prepare_fetch),
        Tool("web_download", (
            "Download a URL into the working folder's downloads/ directory and return the "
            "saved path, size, and SHA-256."),
            {"type": "object", "properties": {
                "url": {"type": "string"}, "filename": {"type": "string"},
                "max_mb": {"type": "integer", "minimum": 1, "maximum": MAX_DOWNLOAD_MB}},
             "required": ["url"], "additionalProperties": False},
            "network", _download, prepare=_prepare_download, writes_files=True),
    ]
