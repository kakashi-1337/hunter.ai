"""The way to the owner's proxy for everything that cannot be handed its login: a plain HTTP proxy on
loopback that carries each connection it is asked for to that proxy, by name, and connects nowhere else.

web_fetch and web_download, the commands of the shell, the knowledge crawler, the model providers and the
proxy test all point an ordinary HTTP client at it. The login stays in this process: a command, and the
model that wrote it, see 127.0.0.1 and a port. An HTTP proxy and a SOCKS5 proxy look the same from here.

What may be asked for is decided before a request is made (the permission rules for a tool call), so the
hop checks no host and looks no name up: the owner's proxy does. The browser has a proxy of its own
(`browser_proxy.Proxy`), which also checks every host a page reaches for.

There is one hop per proxy, started when it is first asked for and kept until the app stops."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time

import httpx

from . import browser_proxy, config
from .tools.base import ToolError
from .upstream import Upstream

REFUSED = browser_proxy.REFUSED      # the header on an answer the hop made itself, with the reason
BAD_LOGIN = "the proxy refused the user name or the password"
# Pages that say where a request came from, asked in this order by the proxy test. HUNTERAI_PROXY_TEST_URL
# names others (comma-separated): `name=value` lines with ip= and loc=, or JSON with "ip" and "country".
PROBES = ("https://www.cloudflare.com/cdn-cgi/trace", "https://api.ipify.org?format=json")
PROBE_ENV = "HUNTERAI_PROXY_TEST_URL"
PROBE_SECONDS = 20.0


class _Hop(browser_proxy.Proxy):
    def __init__(self, up: Upstream):
        super().__init__(on_block=self._refused)
        self.via = up
        self.count, self.why = 0, ""
        self.ready: asyncio.Future | None = None

    def _refused(self, why: str) -> None:
        self.count, self.why = self.count + 1, why.removeprefix(f"the proxy at {self.via.address}: ")

    async def check(self, host: str, port: int) -> str:
        host = host.strip("[]").lower().rstrip(".")
        if not host or not 0 < port < 65536:
            raise ToolError("unusable address")
        return host                  # never connected from here: the owner's proxy is asked for it by name


_hops: dict[Upstream, _Hop] = {}


async def address(up: Upstream) -> str:
    """Where a client finds the hop for this proxy: http://127.0.0.1:<port>. No login is asked there."""
    hop = _hops.get(up)
    if hop is None:
        hop = _hops[up] = _Hop(up)                     # in place before the first wait: a second caller finds this one
        hop.ready = asyncio.ensure_future(hop.start())
    try:
        await asyncio.shield(hop.ready)                # a caller that is cancelled does not stop it for the others
    except OSError:
        _hops.pop(up, None)
        raise
    return f"http://127.0.0.1:{hop.port}"


def refused(up: Upstream) -> tuple[int, str]:
    """(how many connections the proxy did not carry since its hop started, why the last one was not)."""
    hop = _hops.get(up)
    return (hop.count, hop.why) if hop else (0, "")


async def shutdown() -> None:
    hops = list(_hops.values())
    _hops.clear()
    for hop in hops:
        if hop.ready is not None:
            await asyncio.gather(hop.ready, return_exceptions=True)
        await hop.stop()


# ---- what a client of a hop is told, and the test of a proxy

def why(e: Exception) -> str:
    """The reason in a request that failed on its way through a hop, for the owner and the model."""
    said = str(e)
    if isinstance(e, httpx.ProxyError):                # a refused tunnel: the hop put the reason in its status line
        return re.sub(r"^\d{3} ?", "", said).strip() or "the proxy did not carry it"
    if "CERTIFICATE_VERIFY_FAILED" in said:
        return ("the certificate that came back is not the site's own. A proxy that reads https signs it again, and "
                "that is accepted only from a proxy on this machine with its switch on in Settings")
    if isinstance(e, httpx.TimeoutException):
        return "no answer in time"
    return type(e).__name__


def answered_itself(response: httpx.Response) -> str:
    """Why, when this answer was made by the hop or by the proxy and not by the site; "" otherwise."""
    return response.headers.get(REFUSED) or (BAD_LOGIN if response.status_code == 407 else "")


def where(text: str) -> tuple[str, str]:
    """(address, country code) a test page says a request came from; "" for what it does not say."""
    try:
        said = json.loads(text)
    except ValueError:
        said = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        country = said.get("loc") or ""
    else:
        if not isinstance(said, dict):
            return "", ""
        country = said.get("country") or said.get("country_code") or said.get("countryCode") or ""
    try:
        ip = str(ipaddress.ip_address(str(said.get("ip") or "").strip()))
    except ValueError:
        ip = ""
    country = str(country).strip().upper()
    return ip, country if re.fullmatch(r"[A-Z]{2}", country) else ""


def _probes() -> list[str]:
    own = [u.strip() for u in (config.env(PROBE_ENV) or "").split(",") if u.strip()]
    return own or list(PROBES)


async def probe(up: Upstream) -> dict:
    """Send one request through this proxy to a page that says where it came from.
    {"ok": the proxy carried a request, "through": its address, "ms", "ip", "country", "error"}"""
    result = {"ok": False, "through": up.address, "ms": 0, "ip": "", "country": "", "error": ""}
    began = time.monotonic()

    def took() -> int:
        return int((time.monotonic() - began) * 1000)
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            async with httpx.AsyncClient(proxy=await address(up), trust_env=False, follow_redirects=False,
                                         timeout=httpx.Timeout(PROBE_SECONDS, connect=10.0),
                                         verify=not up.presents_its_own_certificates) as client:
                for url in _probes():
                    try:
                        r = await client.get(url)
                    except httpx.HTTPError as e:
                        result["error"] = why(e)
                        if isinstance(e, httpx.ProxyError):
                            break                      # the proxy itself said no: another page changes nothing
                        continue
                    refused = answered_itself(r)
                    if refused:
                        result["error"] = refused
                        break
                    ip, country = where(r.text) if r.status_code == 200 else ("", "")
                    if ip or country:
                        return {**result, "ok": True, "ms": took(), "ip": ip, "country": country, "error": ""}
                    result.update(ok=True, ms=took(), error=f"the proxy carried the request, but {httpx.URL(url).host} "
                                                            f"did not say where it came from (HTTP {r.status_code})")
    except TimeoutError:
        result["error"] = f"no answer within {PROBE_SECONDS:g} seconds"
    except OSError as e:
        result["error"] = f"the local hop could not start ({type(e).__name__})"
    if not result["ok"]:
        result["ms"] = took()
    return result
