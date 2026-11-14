"""Caido, the intercepting proxy, as HunterAI's record of HTTP traffic: what went through it
can be listed, read, and sent again, and the owner can watch the same history in Caido's own
interface.

HunterAI runs its own Caido (the `caido-cli` program, on this machine only, its data under
~/.hunterai/caido) and signs in as a guest. A guest may only keep a temporary project, so
the history lasts as long as that Caido process does: across HunterAI restarts, not across a
reboot. HUNTERAI_CAIDO_URL names a Caido the owner runs instead (it must allow guests).

Caido resolves names itself, so on its own it would connect wherever a name points at that
moment, this machine included. Its way out is therefore HunterAI's filtering proxy
(`egress`), set as Caido's upstream proxy: every connection Caido makes, for the browser, a
replay, or a command, is resolved, checked, and made by that filter."""
from __future__ import annotations

import asyncio
import base64
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from . import browser_proxy, config

DEFAULT_PORT = 8791
PROJECT = "hunterai"
START_WAIT = 30.0            # seconds for a started Caido to answer
REPLAY_WAIT = 90.0           # seconds for a sent request to be answered
TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MAX_ROWS = 100

_state: dict = {"token": None, "egress": None, "routed": None}
_sleep = asyncio.sleep


class CaidoError(Exception):
    """An expected failure, in words for the model and the owner."""


def binary() -> Path | None:
    """The caido-cli program: HUNTERAI_CAIDO_BIN, one on the PATH, or ~/.hunterai/bin/caido-cli."""
    named = os.environ.get("HUNTERAI_CAIDO_BIN")
    for candidate in ((Path(named),) if named else ()) + tuple(
            Path(p) for p in (shutil.which("caido-cli"), config.home() / "bin" / "caido-cli") if p):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def managed() -> bool:
    """Whether HunterAI starts Caido itself (no HUNTERAI_CAIDO_URL)."""
    return not os.environ.get("HUNTERAI_CAIDO_URL")


def port() -> int:
    try:
        return int(os.environ.get("HUNTERAI_CAIDO_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def base_url() -> str:
    return (os.environ.get("HUNTERAI_CAIDO_URL") or f"http://127.0.0.1:{port()}").rstrip("/")


def available() -> bool:
    """Whether there is a Caido to use (HUNTERAI_CAIDO=off: never)."""
    if (os.environ.get("HUNTERAI_CAIDO") or "").lower() == "off":
        return False
    return not managed() or binary() is not None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, trust_env=False, follow_redirects=False)


async def _up(client: httpx.AsyncClient) -> bool:
    try:
        await client.get(base_url() + "/", timeout=httpx.Timeout(3.0, connect=1.0))
        return True
    except httpx.HTTPError:
        return False


def _spawn() -> None:
    """Start caido-cli in its own session, so that it outlives a restart of HunterAI."""
    program = binary()
    if program is None:
        raise CaidoError("caido-cli is not installed: put it on the PATH or at ~/.hunterai/bin/caido-cli")
    data = config.home() / "caido"
    data.mkdir(parents=True, exist_ok=True)
    log = open(config.home() / "caido.log", "ab")       # noqa: SIM115 - handed to the child
    try:
        child = subprocess.Popen(      # noqa: S603 - a fixed command; nothing here comes from a model
            [str(program), "--listen", f"127.0.0.1:{port()}", "--data-path", str(data), "--allow-guests", "--no-open", "--no-sync"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(data),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"), "LANG": "C.UTF-8"})
    except OSError as e:
        raise CaidoError(f"caido-cli could not be started: {type(e).__name__}") from None
    finally:
        log.close()
    (config.home() / "caido.pid").write_text(str(child.pid))


async def _post(client: httpx.AsyncClient, query: str, variables: dict | None, token: str | None) -> dict:
    try:
        r = await client.post(base_url() + "/graphql", json={"query": query, "variables": variables or {}},
                              headers={"Authorization": f"Bearer {token}"} if token else {})
        body = r.json()
    except httpx.HTTPError as e:
        raise CaidoError(f"Caido did not answer ({type(e).__name__})") from None
    except ValueError:
        raise CaidoError(f"Caido answered HTTP {r.status_code} without JSON") from None
    if not isinstance(body, dict):
        raise CaidoError("Caido answered something that is not a GraphQL reply")
    return body


def _error(body: dict) -> tuple[str, str]:
    """(what Caido said, its error code) for a reply that carries errors."""
    errors = body.get("errors") or []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    extra = (first.get("extensions") or {}).get("CAIDO") if isinstance(first.get("extensions"), dict) else None
    extra = extra if isinstance(extra, dict) else {}
    said = str(first.get("message") or "unknown error")
    if extra.get("reason"):
        said += f" ({extra['reason']})"
    return said[:300], str(extra.get("code") or "")


def _why(body: dict) -> str:
    return _error(body)[0]


async def _sign_in(client: httpx.AsyncClient) -> str:
    body = await _post(client, "mutation { loginAsGuest { token { accessToken } error { __typename } } }", None, None)
    got = ((body.get("data") or {}).get("loginAsGuest") or {})
    token = (got.get("token") or {}).get("accessToken")
    if not token:
        raise CaidoError("Caido does not let a guest sign in: start it with --allow-guests"
                         + (f" ({_why(body)})" if body.get("errors") else ""))
    return token


async def _project(client: httpx.AsyncClient, token: str) -> None:
    """Make sure a project is open: the current one, else one named "hunterai" (made if missing)."""
    body = await _post(client, "{ currentProject { project { id } } projects { id name } }", None, token)
    data = body.get("data") or {}
    if (data.get("currentProject") or {}).get("project"):
        return
    pid = next((p["id"] for p in data.get("projects") or [] if p.get("name") == PROJECT), None)
    if pid is None:
        made = await _post(client, "mutation($i: CreateProjectInput!) { createProject(input: $i) { project { id } error { __typename } } }",
                           {"i": {"name": PROJECT, "temporary": True}}, token)
        pid = ((((made.get("data") or {}).get("createProject") or {}).get("project")) or {}).get("id")
        if pid is None:
            raise CaidoError("Caido would not create a project for HunterAI")
    chosen = await _post(client, "mutation($id: ID!) { selectProject(id: $id) { currentProject { project { id } } error { __typename } } }",
                         {"id": pid}, token)
    if not (((chosen.get("data") or {}).get("selectProject") or {}).get("currentProject")):
        raise CaidoError("Caido would not open HunterAI's project")


class _LocalOk:
    """The local (host, port) pairs Caido's way out lets through: the ones a browser session
    was allowed, as long as that session lives, and the ones a send was allowed, while it runs."""

    def __init__(self):
        self.own: set[tuple[str, int]] = set()

    def __contains__(self, pair) -> bool:
        from . import browser
        return pair in self.own or any(pair in s.local_ok for s in browser.sessions())


async def egress() -> browser_proxy.Proxy:
    """Caido's way out: one filtering proxy for this run of HunterAI."""
    if _state["egress"] is None:
        proxy = browser_proxy.Proxy()
        proxy.local_ok = _LocalOk()
        await proxy.start()
        _state["egress"] = proxy
    return _state["egress"]


async def shutdown() -> None:
    proxy, _state["egress"], _state["routed"] = _state["egress"], None, None
    if proxy is not None:
        await proxy.stop()


async def _route(client: httpx.AsyncClient, token: str) -> None:
    """Make HunterAI's filter the upstream proxy of Caido, and drop the ones left by earlier runs."""
    out = (await egress()).port
    body = await _post(client, "{ upstreamProxiesHttp { id enabled allowlist connection { host port } } }", None, token)
    if body.get("errors") or body.get("data") is None:
        raise CaidoError("Caido would not show its upstream proxies: " + _why(body))
    have = False
    for p in (body["data"].get("upstreamProxiesHttp") or []):
        conn = p.get("connection") or {}
        if conn.get("host") != "127.0.0.1" or p.get("allowlist") != ["*"]:
            continue                             # not one of ours: the owner's own setting is left alone
        if conn.get("port") == out and p.get("enabled") and not have:
            have = True
            continue
        await _post(client, "mutation($id: ID!) { deleteUpstreamProxyHttp(id: $id) { deletedId } }", {"id": p["id"]}, token)
    if not have:
        made = await _post(client, "mutation($i: CreateUpstreamProxyHttpInput!) { createUpstreamProxyHttp(input: $i) { proxy { id } } }",
                           {"i": {"enabled": True, "connection": {"host": "127.0.0.1", "port": out, "isTLS": False},
                                  "allowlist": ["*"], "denylist": []}}, token)
        if not (((made.get("data") or {}).get("createUpstreamProxyHttp") or {}).get("proxy")):
            raise CaidoError("Caido would not take HunterAI's filter as its upstream proxy: " + _why(made))
    _state["routed"] = out


async def ensure(client: httpx.AsyncClient) -> str:
    """A running Caido, a signed-in guest, an open project, HunterAI's filter as its way out.
    Returns the access token."""
    if not await _up(client):
        if not managed():
            raise CaidoError(f"Caido does not answer at {base_url()}")
        _state["token"] = None
        _spawn()
        until = time.monotonic() + START_WAIT
        while not await _up(client):
            if time.monotonic() >= until:
                raise CaidoError(f"Caido was started but does not answer at {base_url()}; see ~/.hunterai/caido.log")
            await _sleep(0.25)
    if _state["token"] is None:
        token = await _sign_in(client)
        await _project(client, token)
        await _route(client, token)
        _state["token"] = token
    else:
        # Looked at every time: Caido keeps this setting, and another program (or the owner, in Caido) can change it.
        try:
            await _route(client, _state["token"])
        except CaidoError:
            _state["token"] = None           # Caido was restarted since the sign-in: sign in again, once
            return await ensure(client)
    return _state["token"]


async def gql(query: str, variables: dict | None = None, client: httpx.AsyncClient | None = None) -> dict:
    """The `data` of a GraphQL reply. A refused token is replaced once (Caido was restarted)."""
    own = client is None
    client = client or _client()
    try:
        for attempt in (0, 1):
            had = _state["token"]
            token = await ensure(client)
            body = await _post(client, query, variables, token)
            if not body.get("errors") and body.get("data") is not None:
                return body["data"]
            if attempt == 0 and had is not None and _error(body)[1] == "AUTHORIZATION":
                _state["token"] = None           # Caido was restarted since the sign-in: sign in again and ask once more
                continue
            raise CaidoError(_why(body))
        raise CaidoError("Caido refused the request")
    finally:
        if own:
            await client.aclose()


async def refresh() -> bool:
    """At HunterAI's start: a Caido that is already running gets this run's filter as its way
    out (the one of the run before is gone). Caido is not started here. False when it is not up."""
    if not available():
        return False
    client = _client()
    try:
        if not await _up(client):
            return False
        await ensure(client)
        return True
    except CaidoError:
        return False
    finally:
        await client.aclose()


async def proxy_address() -> tuple[str, int]:
    """(host, port) of Caido's proxy, with Caido running and its way out in place."""
    from urllib.parse import urlsplit
    client = _client()
    try:
        await ensure(client)
    finally:
        await client.aclose()
    parts = urlsplit(base_url())
    return parts.hostname or "127.0.0.1", parts.port or (443 if parts.scheme == "https" else 80)


def _blob(value) -> bytes:
    try:
        return base64.b64decode(value or "")
    except (ValueError, TypeError):
        return b""


ROW = "id host port method path query isTls source createdAt response { id statusCode length roundtripTime }"


async def status() -> dict:
    data = await gql("{ runtime { version } currentProject { project { name temporary } } requestsByOffset(limit: 1, offset: 0) { count { value } } }")
    project = ((data.get("currentProject") or {}).get("project")) or {}
    return {"url": base_url(), "version": (data.get("runtime") or {}).get("version"), "project": project.get("name"),
            "temporary": bool(project.get("temporary")), "requests": ((data.get("requestsByOffset") or {}).get("count") or {}).get("value"),
            "managed": managed()}


async def history(filter_code: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    """(rows, how many match): the newest first. `filter_code` is HTTPQL."""
    limit = max(1, min(int(limit), MAX_ROWS))
    data = await gql("query($l: Int, $o: Int, $f: HTTPQLInput) { requestsByOffset(limit: $l, offset: $o, filter: $f, "
                     "order: {by: ID, ordering: DESC}) { count { value } nodes { " + ROW + " raw } } }",
                     {"l": limit, "o": max(0, int(offset)), "f": {"code": filter_code} if filter_code else None})
    got = data.get("requestsByOffset") or {}
    rows = list(got.get("nodes") or [])
    for r in rows:
        r["raw"] = _blob(r.get("raw"))
    return rows, int((got.get("count") or {}).get("value") or 0)


async def exchange(request_id: str) -> dict | None:
    """One recorded request with its raw bytes and those of its response; None when there is none."""
    data = await gql("query($id: ID!) { request(id: $id) { " + ROW + " raw response { raw } } }", {"id": str(request_id)})
    r = data.get("request")
    if not r:
        return None
    r["raw"] = _blob(r.get("raw"))
    if r.get("response"):
        r["response"]["raw"] = _blob(r["response"].get("raw"))
    return r


async def replay(host: str, port_: int, tls: bool, sni: str | None, raw: bytes) -> dict:
    """Send `raw` to host:port through Caido and wait for the answer. The request is kept in
    Caido's history and under Replay. Returns {"request_id", "status", "raw", "error"}."""
    connection = {"host": host, "port": int(port_), "isTLS": bool(tls), **({"SNI": sni} if sni and tls else {})}
    client = _client()
    try:
        made = await gql("mutation($i: CreateReplaySessionInput!) { createReplaySession(input: $i) { session { id } error { __typename } } }",
                         {"i": {"kind": "HTTP", "requestSource": {"raw": {"connectionInfo": connection, "raw": base64.b64encode(raw).decode()}}}}, client)
        session = ((made.get("createReplaySession") or {}).get("session") or {}).get("id")
        if session is None:
            raise CaidoError("Caido would not take the request for replay")
        started = await gql("mutation($id: ID!) { startReplayTask(sessionId: $id) { task { id replayEntry { id } } error { __typename } } }",
                            {"id": session}, client)
        entry = (((started.get("startReplayTask") or {}).get("task") or {}).get("replayEntry") or {}).get("id")
        if entry is None:
            raise CaidoError("Caido would not send the request")
        until = time.monotonic() + REPLAY_WAIT
        while True:
            data = await gql("query($id: ID!) { replayEntry(id: $id, sessionKind: HTTP) { error ... on ReplayEntryHttp { "
                             "request { id response { statusCode raw } } } } }", {"id": entry}, client)
            got = data.get("replayEntry") or {}
            request = got.get("request") or {}
            response = request.get("response")
            if got.get("error") or response:
                return {"request_id": request.get("id"), "status": (response or {}).get("statusCode"),
                        "raw": _blob((response or {}).get("raw")), "error": got.get("error")}
            if time.monotonic() >= until:
                return {"request_id": request.get("id"), "status": None, "raw": b"", "error": f"no answer within {REPLAY_WAIT:g} s"}
            await _sleep(0.3)
    finally:
        await client.aclose()
