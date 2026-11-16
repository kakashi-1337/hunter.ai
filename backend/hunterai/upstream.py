"""The owner's own proxy for the agent's web traffic: Burp, mitmproxy, ZAP, or a tunnel that hides where
the requests come from.

With one set, web_fetch, web_download and the browser tools send their requests through it instead of
connecting themselves. HunterAI still resolves and checks every host first (scope, local addresses, its
own API); the proxy is then asked for the host by name, so where the proxy connects is the proxy's own
doing. When the proxy does not answer, nothing is sent directly: a proxy set to hide an address must
not fall back to showing it.

Shell commands are not sent through it. Caido has its own switch, and the two are not combined."""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import db
from .repo import settings
from .tools import resolve


class ProxyError(ValueError):
    """An address that is refused, with the reason for the owner."""


def on_this_machine(host: str) -> bool:
    return host == "localhost" or (resolve.is_ip(host) and resolve.is_loopback(host))


@dataclass(frozen=True)
class Upstream:
    host: str
    port: int
    intercepts: bool = False          # the owner says it reads https and answers with its own certificates

    @property
    def address(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"http://{self.address}"

    @property
    def presents_its_own_certificates(self) -> bool:
        """Only a proxy on this machine is believed when it does: one elsewhere must pass the site's on."""
        return self.intercepts and on_this_machine(self.host)


def parse(text: str | None) -> tuple[str, int] | None:
    """(host, port) of an HTTP proxy, None for an empty field. ProxyError says what is wrong otherwise."""
    s = (text or "").strip()
    if not s:
        return None
    scheme = re.match(r"^([a-z][a-z0-9+.-]*)://", s, re.I)
    rest = s[scheme.end():] if scheme else s
    name = scheme.group(1).lower() if scheme else "http"
    if name.startswith("socks"):
        raise ProxyError("a SOCKS proxy is not supported: put an HTTP proxy in front of it (Burp and mitmproxy "
                         "can pass their traffic on to one)")
    if name != "http":
        raise ProxyError("give the address of an HTTP proxy, like http://127.0.0.1:8080 (it carries https too; "
                         "its own address starts with http)")
    if "@" in rest:
        raise ProxyError("a proxy that needs a user name and a password is not supported")
    rest = rest[:-1] if rest.endswith("/") else rest
    if re.search(r"[/?#]", rest):
        raise ProxyError("give only the host and its port, like 127.0.0.1:8080")
    host, colon, port = rest.rpartition(":")
    if not colon or not port.isdigit() or not 0 < int(port) < 65536:
        raise ProxyError("give the port of the proxy too, like 127.0.0.1:8080")
    try:
        return resolve.normalize_host(host), int(port)
    except ValueError:
        raise ProxyError("that is not a host name or an address") from None


async def get(conn) -> Upstream | None:
    try:
        address = parse(await settings.get(conn, "upstream_proxy", ""))
    except ProxyError:
        return None                   # a stored value from another version: as if none were set
    if address is None:
        return None
    return Upstream(*address, intercepts=bool(await settings.get(conn, "upstream_proxy_intercepts", False)))


async def load() -> Upstream | None:
    """The proxy that is set, for a tool that has no connection of its own."""
    conn = await db.connect()
    try:
        return await get(conn)
    finally:
        await conn.close()
