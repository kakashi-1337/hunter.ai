"""The browser's only way out: a small forward proxy on loopback that every request of a
browser session goes through, redirects, WebSockets, and service workers included.

For each connection it resolves the host itself, refuses addresses on this machine or the
local network (unless the owner allowed that exact host and port), refuses HunterAI's own
API, and connects to the address it checked. The browser never resolves a name on its own,
so a name cannot answer one thing to the check and another to the connection.

With `via` set, a checked connection is handed to another proxy by name instead of being made
here: Caido, which records it, or the owner's own proxy, with its login. Caido's own way out is
a second Proxy of this kind, which checks again and connects to the address it checked: see
`caido.egress`. When the proxy in `via` does not carry a connection, the answer says why and
nothing is connected from here instead."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from . import upstream as owners_proxy
from .tools import resolve
from .tools.base import HostUse, ToolError

MAX_HEAD = 64 * 1024
CONNECT_TIMEOUT = 15.0
IDLE_TIMEOUT = 300.0
_HOP = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}
REFUSED = "X-HunterAI-Refused"       # on every answer this proxy makes itself: why, so it is not taken for the site's


async def _resolve(host: str) -> tuple[str, ...]:
    if resolve.is_ip(host):
        return (host,)
    if host == "localhost" or host.endswith(".localhost"):
        return ("127.0.0.1",)
    return await resolve.resolve_ips(host)


class Proxy:
    """One per browser session. `local_ok` holds the (host, port) pairs the owner allowed
    although they are local; `on_block(text)` is told about every refused connection."""

    def __init__(self, on_block: Callable[[str], None] | None = None):
        self.via: owners_proxy.Upstream | None = None      # the proxy that carries what this one let through
        self.local_ok: set[tuple[str, int]] = set()
        self.on_block = on_block or (lambda text: None)
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for t in list(self._tasks):
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(self._server.wait_closed(), 5)
            except asyncio.TimeoutError:
                pass
            self._server = None

    async def check(self, host: str, port: int) -> str:
        """The address to connect to for this host and port. ToolError with the reason when refused."""
        host = host.strip("[]").lower().rstrip(".")
        if not host or not 0 < port < 65536:
            raise ToolError("unusable address")
        ips = await _resolve(host)
        if not ips:
            raise ToolError(f"{host} does not resolve")
        if resolve.is_own_api(HostUse(host, port, tuple(ips))) or any(
                resolve.is_own_api(HostUse(ip, port, (ip,))) for ip in ips):
            raise ToolError("HunterAI's own address")
        if any(resolve.is_local(ip) for ip in ips) and (host, port) not in self.local_ok:
            raise ToolError(f"{host}:{port} is an address on this machine or local network")
        return ips[0]

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        upstream = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            if len(head) > MAX_HEAD:
                raise ToolError("request head too large")
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = (lines[0].split(" ", 2) + ["", ""])[:3]
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                ip = await self.check(host, int(port) if port.isdigit() else 0)
                if self.via is None:
                    upstream = await asyncio.wait_for(asyncio.open_connection(ip, int(port)), CONNECT_TIMEOUT)
                else:           # the carrying proxy is asked for the host by name, with its login if it has one
                    upstream = await owners_proxy.open_tunnel(self.via, host.strip("[]"), int(port), CONNECT_TIMEOUT)
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise ToolError("only http and https are carried")
                port = parts.port or 80
                ip = await self.check(parts.hostname, port)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                login = None
                if self.via is None:
                    upstream = await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
                elif self.via.scheme == "socks5":       # a tunnel to the site, and the request as the site takes it
                    upstream = await owners_proxy.open_tunnel(self.via, parts.hostname, port, CONNECT_TIMEOUT)
                else:                                   # an HTTP proxy is asked with the whole address
                    upstream = await owners_proxy.reach(self.via, CONNECT_TIMEOUT)
                    path = f"http://{parts.netloc.rpartition('@')[2]}{path}"
                    login = owners_proxy.basic(self.via)
                upgrade = any(line.lower().startswith("upgrade:") for line in lines[1:])
                kept = [line for line in lines[1:] if line and line.split(":", 1)[0].strip().lower() not in _HOP]
                if login:
                    kept.append(f"Proxy-Authorization: {login}")
                # One request per connection: the next one may be for another host and must be checked too.
                kept.append("Connection: Upgrade" if upgrade else "Connection: close")
                upstream[1].write((f"{method} {path} {version or 'HTTP/1.1'}\r\n" + "\r\n".join(kept) + "\r\n\r\n").encode("latin-1"))
                await upstream[1].drain()
            await self._pipe(reader, writer, *upstream)
        except ToolError as e:
            self.on_block(str(e))
            await self._refuse(writer, str(e))
        except owners_proxy.ProxyFailed as e:           # only ever raised for the proxy in `via`
            self.on_block(f"the proxy at {self.via.address}: {e}")
            await self._refuse(writer, str(e), 502)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError, ValueError):
            await self._refuse(writer, "upstream unavailable", 502)
        except asyncio.CancelledError:
            pass
        finally:
            for w in (writer, upstream[1] if upstream else None):
                if w is not None:
                    try:
                        w.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._tasks.discard(task)

    @staticmethod
    async def _refuse(writer: asyncio.StreamWriter, why: str, status: int = 403) -> None:
        body = f"Blocked by HunterAI: {why}\n".encode()     # a whole line: a command prints it as it comes
        said = re.sub(r"[^\x20-\x7e]", "?", why)[:300]      # one line of plain characters: it travels in the head
        try:                 # a failed proxy is named in the status line too: that is all a client shows of a refused tunnel
            writer.write(f"HTTP/1.1 {status} {said if status == 502 else 'Blocked'}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                         f"{REFUSED}: {said}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        except (OSError, ConnectionError):
            pass

    @staticmethod
    async def _pipe(cr, cw, ur, uw) -> None:
        async def copy(src, dst):
            try:
                while True:
                    data = await asyncio.wait_for(src.read(65536), IDLE_TIMEOUT)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except (asyncio.TimeoutError, OSError, ConnectionError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass
        await asyncio.gather(copy(cr, uw), copy(ur, cw))
