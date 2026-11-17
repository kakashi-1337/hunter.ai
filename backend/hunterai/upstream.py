"""The owner's own proxy for the agent's traffic: Burp, mitmproxy, ZAP, a tunnel to another country,
or a proxy service a program asks its hunters to come through.

There is one in Settings, the default, and a target can have its own, for the one program that needs
it. With one in effect, web_fetch, web_download, the browser tools and the shell's commands send
through it, and knowledge crawls do when the owner switched that on. HunterAI still resolves and checks
every host first (scope, local addresses, its own API); the proxy is then asked for the host by name, so
where the proxy connects is the proxy's own doing. When the proxy does not carry a connection, nothing
is sent directly: a proxy set to hide an address must not fall back to showing it.

An address is `http://` or `socks5://`, with a user name and a password where the proxy asks for
them. The password is stored with the address and is never shown again, to the owner or to the model:
everything but the browser reaches the proxy through a hop on loopback that holds the login (`hop.py`),
and `secrets` masks a password in what a tool returns.

The model providers have a proxy of their own, in the env file with their keys (`of_providers`).
Caido has its own switch, and the two are not combined."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from . import config, db
from .repo import settings
from .tools import resolve

PROVIDER_ENV = "HUNTERAI_PROVIDER_PROXY"      # in the env file, next to the keys it serves; Settings writes it there
SCHEMES = {"http": "http", "socks5": "socks5", "socks5h": "socks5"}      # a name is always resolved by the proxy


class ProxyError(ValueError):
    """An address that is refused, with the reason for the owner."""


class ProxyFailed(OSError):
    """The proxy did not carry a connection: nothing answers at its address, or it answered and said no
    (a wrong login, a host it would not or could not reach). The text says which and never holds the login."""


_logins: set[tuple[str, str]] = set()


def logins() -> set[tuple[str, str]]:
    """(user name, password) of every proxy this process has read: `secrets` masks a login where one
    shows up in what a tool returns, should a command ever print a stored address."""
    return set(_logins)


def on_this_machine(host: str) -> bool:
    return host == "localhost" or (resolve.is_ip(host) and resolve.is_loopback(host))


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    intercepts: bool = False          # the owner says it reads https and answers with its own certificates
    scheme: str = "http"              # http | socks5
    user: str = ""
    password: str = field(default="", repr=False)

    @property
    def address(self) -> str:
        """Host and port, for messages: no user name, no password."""
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """The whole address, password included: what is stored and what a client is given."""
        login = quote(self.user, safe="") + (":" + quote(self.password, safe="") if self.password else "") + "@" if self.user else ""
        return f"{self.scheme}://{login}{self.address}"

    @property
    def shown(self) -> str:
        """The address as it is shown again: with the user name, without the password."""
        return f"{self.scheme}://{quote(self.user, safe='') + '@' if self.user else ''}{self.address}"

    @property
    def presents_its_own_certificates(self) -> bool:
        """Only a proxy on this machine is believed when it does: one elsewhere must pass the site's on."""
        return self.intercepts and on_this_machine(self.host)

    def same_login(self, other: "Upstream | None") -> bool:
        return other is not None and (self.scheme, self.user, self.host, self.port) == (other.scheme, other.user, other.host, other.port)


def parse(text: str | None) -> Upstream | None:
    """The proxy an address names, None for an empty field. ProxyError says what is wrong otherwise."""
    s = (text or "").strip()
    if not s:
        return None
    scheme = re.match(r"^([a-z][a-z0-9+.-]*)://", s, re.I)
    rest = s[scheme.end():] if scheme else s
    name = SCHEMES.get(scheme.group(1).lower() if scheme else "http")
    if name is None:
        raise ProxyError("an address starts with http:// or socks5:// (an HTTP proxy carries https too; its own "
                         "address starts with http)")
    login, at, rest = rest.rpartition("@")
    user = password = ""
    if at:
        raw_user, _, raw_password = login.partition(":")
        user, password = unquote(raw_user), unquote(raw_password)
        if not user or re.search(r"[\s\x00-\x1f\x7f]", user):
            raise ProxyError("the user name before the @ is empty or holds a space")
        if re.search(r"[\x00-\x1f\x7f]", password):
            raise ProxyError("the password holds a character that cannot be sent")
    rest = rest[:-1] if rest.endswith("/") else rest
    if re.search(r"[/?#]", rest):
        raise ProxyError("give only the host and its port, like 127.0.0.1:8080")
    host, colon, port = rest.rpartition(":")
    if not colon or not port.isdigit() or not 0 < int(port) < 65536:
        raise ProxyError("give the port of the proxy too, like 127.0.0.1:8080")
    try:
        up = Upstream(resolve.normalize_host(host), int(port), scheme=name, user=user, password=password)
    except ValueError:
        raise ProxyError("that is not a host name or an address") from None
    if password:
        _logins.add((user, password))
    return up


def typed(text: str | None, was: Upstream | None) -> Upstream | None:
    """What a form sent. The form is shown the address without its password, so an address that comes back
    with the same proxy and user name and no password keeps the password that is stored."""
    up = parse(text)
    if up is not None and up.user and not up.password and up.same_login(was):
        return Upstream(up.host, up.port, scheme=up.scheme, user=up.user, password=was.password)
    return up


def _stored(url: str | None, intercepts: bool = False) -> Upstream | None:
    try:
        up = parse(url)
    except ProxyError:
        return None                   # a stored value from another version: as if none were set
    if up is None:
        return None
    return Upstream(up.host, up.port, intercepts, up.scheme, up.user, up.password)


async def get(conn) -> Upstream | None:
    """The default, from Settings."""
    return _stored(await settings.get(conn, "upstream_proxy", ""), bool(await settings.get(conn, "upstream_proxy_intercepts", False)))


async def of_target(conn, target_id: str | None) -> Upstream | None:
    """The target's own proxy, None when it has none."""
    if not target_id:
        return None
    r = await (await conn.execute("select url from target_proxies where target_id=?", (target_id,))).fetchone()
    return _stored(r["url"]) if r else None


async def of_targets(conn) -> dict[str, Upstream]:
    """Every target that has a proxy of its own, by target id."""
    rows = await (await conn.execute("select target_id, url from target_proxies")).fetchall()
    return {r["target_id"]: up for r in rows if (up := _stored(r["url"])) is not None}


async def put_target(conn, target_id: str, text: str | None) -> Upstream | None:
    """Set or, with an empty text, clear the target's own proxy. ProxyError for an address that is refused."""
    up = typed(text, await of_target(conn, target_id))
    if up is None:
        await conn.execute("delete from target_proxies where target_id=?", (target_id,))
    else:
        await conn.execute("insert into target_proxies(target_id,url) values(?,?) on conflict(target_id) do update set url=excluded.url",
                           (target_id, up.url))
    await conn.commit()
    return up


async def in_effect(conn, target_id: str | None) -> Upstream | None:
    """The proxy a chat on this target sends through: the target's own, else the default."""
    return await of_target(conn, target_id) or await get(conn)


def of_providers() -> Upstream | None:
    """The proxy the model providers are reached through, None when there is none. It is a setting of its
    own: a proxy a bug bounty program asks for is not where the conversation with a model should go.
    ProxyError when what stands in the env file is not an address."""
    return parse(config.env(PROVIDER_ENV))


async def remember(conn) -> None:
    """Read every stored address once, so `secrets` knows each password before the first tool runs."""
    await get(conn)
    for r in await (await conn.execute("select url from target_proxies")).fetchall():
        _stored(r["url"])
    try:
        of_providers()
    except ProxyError:
        pass


async def of_crawls(conn) -> Upstream | None:
    """The proxy knowledge crawls go through: the one in Settings, and only when the owner switched it on
    for them. A crawl reads public pages for the knowledge base; it is not work on a target."""
    if not await settings.get(conn, "upstream_proxy_kb", False):
        return None
    return await get(conn)


async def for_crawls() -> Upstream | None:
    """`of_crawls`, for the crawler, which has no connection of its own."""
    conn = await db.connect()
    try:
        return await of_crawls(conn)
    finally:
        await conn.close()


async def load(target_id: str | None = None) -> Upstream | None:
    """The proxy in effect, for a tool that has no connection of its own."""
    conn = await db.connect()
    try:
        return await in_effect(conn, target_id)
    finally:
        await conn.close()


# ---- what a command and the model are given

NOT_PROXIED = "localhost,127.0.0.1,::1"       # this machine is reached directly: a proxy elsewhere cannot see it


def variables(hop_address: str) -> dict[str, str]:
    """The environment that sends a command's traffic to the hop at `hop_address`, in every spelling a
    program may read (curl reads the lower-case http_proxy only)."""
    env = {n: hop_address for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") for n in (name, name.lower())}
    env["NO_PROXY"] = env["no_proxy"] = NOT_PROXIED
    return env


def rule(up: Upstream | None, tool_names) -> str:
    """What the model is told when a proxy is in effect for its chat: the address, never the login."""
    if up is None or not tool_names:
        return ""
    text = (f"Proxy: the traffic to this chat's target goes out through the owner's proxy at {up.address}. "
            "web_fetch, web_download and the browser tools use it on their own. When the proxy does not carry a "
            "connection, the tool says so. Nothing is sent directly then: report it to the owner, and do not look "
            "for another way out.")
    if "execute" in tool_names:
        text += (" Commands run with HTTP_PROXY, HTTPS_PROXY and ALL_PROXY (and the lower-case names) set to a "
                 "local address that leads to the proxy: curl, wget and most HTTP libraries follow them without "
                 "being told. Give a scanner that address when it has a flag for one, for example "
                 'ffuf -x "$HTTP_PROXY", nuclei -proxy "$HTTP_PROXY", sqlmap --proxy="$HTTP_PROXY". Do not unset or '
                 "replace these variables. A program that cannot speak through an HTTP proxy (nmap, masscan, ping, "
                 "dig, nc, ssh) connects from this machine's own address: ask the owner before running one "
                 "against the target.")
    return text


# ---- a connection made by the proxy, for the parts of HunterAI that speak to it themselves

def basic(up: Upstream) -> str | None:
    """The Proxy-Authorization value an HTTP proxy is given, None when there is no login."""
    if not up.user:
        return None
    return "Basic " + base64.b64encode(f"{up.user}:{up.password}".encode()).decode()


async def reach(up: Upstream, timeout: float = 15.0):
    """(reader, writer) of a connection to the proxy itself. ProxyFailed when nothing answers there."""
    try:
        return await asyncio.wait_for(asyncio.open_connection(up.host, up.port), timeout)
    except OSError as e:              # a refused connection, an unknown name, and a wait that ran out
        raise ProxyFailed(f"nothing answers at the proxy's address ({type(e).__name__})") from None


async def open_tunnel(up: Upstream, host: str, port: int, timeout: float = 15.0):
    """(reader, writer) of a connection to host:port that the proxy made. The proxy is given the host as
    it was named, so it is the proxy that looks the name up. ProxyFailed (an OSError) when the proxy cannot
    be reached or says no; nothing is ever connected directly from here."""
    reader, writer = await reach(up, timeout)
    try:
        await asyncio.wait_for((_socks5 if up.scheme == "socks5" else _connect)(up, reader, writer, host, port), timeout)
    except BaseException as e:
        writer.close()
        if isinstance(e, TimeoutError):
            raise ProxyFailed("the proxy did not answer in time") from None
        if isinstance(e, OSError) and not isinstance(e, ProxyFailed):
            raise ProxyFailed(f"the connection to the proxy broke ({type(e).__name__})") from None
        raise
    return reader, writer


async def _connect(up: Upstream, reader, writer, host: str, port: int) -> None:
    name = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    lines = [f"CONNECT {name} HTTP/1.1", f"Host: {name}"]
    if up.user:
        lines.append(f"Proxy-Authorization: {basic(up)}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    await writer.drain()
    try:
        status = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n", 1)[0].split(b" ")[1:2]
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise ProxyFailed("the proxy closed the connection without an answer") from None
    if status == [b"407"]:
        raise ProxyFailed("the proxy refused the user name or the password")
    if status != [b"200"]:
        raise ProxyFailed(f"the proxy refused the tunnel to {host} ({b' '.join(status).decode('latin-1') or 'no status'})")


_SOCKS_REPLIES = {1: "general failure", 2: "not allowed by its rules", 3: "network unreachable", 4: "host unreachable",
                  5: "connection refused", 6: "timed out", 7: "command not supported", 8: "address type not supported"}


async def _socks5(up: Upstream, reader, writer, host: str, port: int) -> None:
    """RFC 1928, with the user name and password of RFC 1929 when the proxy asks for them."""
    try:
        writer.write(b"\x05\x02\x00\x02" if up.user else b"\x05\x01\x00")
        await writer.drain()
        _, method = await reader.readexactly(2)
        if method == 2:
            user, password = up.user.encode(), up.password.encode()
            if not user or len(user) > 255 or len(password) > 255:
                raise ProxyFailed("the proxy wants a user name and a password")
            writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            await writer.drain()
            _, status = await reader.readexactly(2)
            if status != 0:
                raise ProxyFailed("the proxy refused the user name or the password")
        elif method != 0:
            raise ProxyFailed("the proxy wants a user name and a password" if not up.user
                               else "the proxy takes no login that HunterAI can give")
        try:
            ip = ipaddress.ip_address(host)
            where = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        except ValueError:
            name = host.encode("idna")
            where = b"\x03" + bytes([len(name)]) + name
        writer.write(b"\x05\x01\x00" + where + port.to_bytes(2, "big"))
        await writer.drain()
        _, reply, _, kind = await reader.readexactly(4)
        if reply != 0:
            raise ProxyFailed(f"the proxy could not reach {host}: {_SOCKS_REPLIES.get(reply, f'reply {reply}')}")
        await reader.readexactly({1: 4, 4: 16}.get(kind) or (await reader.readexactly(1))[0])
        await reader.readexactly(2)
    except asyncio.IncompleteReadError:
        raise ProxyFailed("the proxy closed the connection without an answer") from None
