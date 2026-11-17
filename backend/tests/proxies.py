"""Stand-ins on loopback for the tests of the owner's proxy: a site, an HTTP proxy and a SOCKS5 proxy.
Both proxies carry every host they are asked for to the site, so a test can use a name that does not exist,
and both write down what they were asked."""
import asyncio
import base64
from contextlib import asynccontextmanager


async def _head(reader) -> list[str]:
    return (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")


async def _pipe(a_reader, a_writer, b_reader, b_writer) -> None:
    async def copy(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            w.close()
    await asyncio.gather(copy(a_reader, b_writer), copy(b_reader, a_writer))


class Site:
    """Answers every request with the host it was asked for and the path, or with what `answer(host, path)` says."""

    def __init__(self, answer=None, ctype="text/plain"):
        self.answer = answer or (lambda host, path: f"site says: {host}{path}")
        self.ctype = ctype
        self.requests: list[tuple[str, str, str]] = []           # (method, path, host header)
        self.heads: list[str] = []                               # every request head as it arrived, lower case
        self.port = 0

    async def _serve(self, reader, writer):
        try:
            lines = await _head(reader)
            method, path, _ = lines[0].split(" ", 2)
            host = next((line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("host:")), "")
            self.requests.append((method, path, host))
            self.heads.append("\n".join(lines).lower())
            body = self.answer(host, path).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {self.ctype}\r\nConnection: close\r\nContent-Length: {len(body)}"
                         "\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


class HttpProxy:
    """An HTTP proxy. With a user name it wants Basic credentials, and answers 407 to anything else."""

    def __init__(self, site: Site, user: str = "", password: str = ""):
        self.site, self.user, self.password = site, user, password
        self.asked: list[tuple[str, str]] = []                   # (method, what it was asked for)
        self.port = 0

    async def _serve(self, reader, writer):
        try:
            lines = await _head(reader)
            method, target, version = lines[0].split(" ", 2)
            auth = next((line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("proxy-authorization:")), "")
            want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            if self.user and auth != want:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"proxy\"\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            self.asked.append((method, target))
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.site.port)
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:                                                # an absolute address: pass the request on as a path
                path = "/" + target.split("://", 1)[1].split("/", 1)[1] if "/" in target.split("://", 1)[1] else "/"
                kept = [line for line in lines[1:] if line and not line.lower().startswith("proxy-")]
                up_writer.write((f"{method} {path} {version}\r\n" + "\r\n".join(kept) + "\r\n\r\n").encode("latin-1"))
                await up_writer.drain()
            await _pipe(reader, writer, up_reader, up_writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


class Socks5Proxy:
    """A SOCKS5 proxy (RFC 1928). With a user name it wants that name and password (RFC 1929)."""

    def __init__(self, site: Site, user: str = "", password: str = ""):
        self.site, self.user, self.password = site, user, password
        self.asked: list[tuple[str, int]] = []                   # (host as it was named, port)
        self.logins: list[tuple[str, str]] = []
        self.port = 0

    async def _serve(self, reader, writer):
        try:
            version, count = await reader.readexactly(2)
            methods = await reader.readexactly(count)
            if self.user:
                if 2 not in methods:
                    writer.write(b"\x05\xff")
                    return
                writer.write(b"\x05\x02")
                await reader.readexactly(1)
                user = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
                password = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
                self.logins.append((user, password))
                ok = (user, password) == (self.user, self.password)
                writer.write(b"\x01\x00" if ok else b"\x01\x01")
                await writer.drain()
                if not ok:
                    return
            else:
                writer.write(b"\x05\x00")
            _, command, _, kind = await reader.readexactly(4)
            if kind == 3:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            elif kind == 1:
                host = ".".join(str(b) for b in await reader.readexactly(4))
            else:
                host = (await reader.readexactly(16)).hex()
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.asked.append((host, port))
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self.site.port)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            await _pipe(reader, writer, up_reader, up_writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@asynccontextmanager
async def running(*servers):
    """Start each stand-in on a free loopback port; stop them all at the end."""
    started = []
    try:
        for s in servers:
            server = await asyncio.start_server(s._serve, "127.0.0.1", 0)
            s.port = server.sockets[0].getsockname()[1]
            started.append(server)
        yield servers
    finally:
        for server in started:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in started), return_exceptions=True)
