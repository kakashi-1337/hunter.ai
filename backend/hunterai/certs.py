"""A site's certificate, checked by HunterAI itself.

With Caido in between, the tool that asks (web_fetch, web_download, caido_send, the browser) is shown Caido's
certificate, and Caido takes a site's certificate much as it comes: one that no authority signed, and one that
has expired, are passed on without a word (seen with a real Caido against badssl.com; only a certificate for
another name was refused). So before a request to an https site is handed to Caido, HunterAI makes a TLS
handshake with the site itself, at the address it checked, and goes by that. Nothing is sent in it. A
certificate that does not verify stops the call, as it would without Caido, and so does a site whose
certificate cannot be seen: what could not be checked is not taken for good.

A certificate found good is not asked for again for KEEP seconds; a bad one is asked for every time."""
from __future__ import annotations

import asyncio
import ssl
import time

from .tools.base import ToolError

KEEP = 600.0                 # seconds a certificate that was found good stays good
WAIT = 10.0                  # seconds for the handshake

_good: dict[tuple[str, str, int], float] = {}
_now = time.monotonic
HOW_ELSE = ("While it records in Caido, HunterAI checks a site's certificate itself: Caido passes one like this on. "
            "To work with this site all the same, use curl -k in a command.")


def _context() -> ssl.SSLContext:
    """The authorities web_fetch trusts when it connects by itself, so that the two agree on what is good."""
    import httpx
    return httpx.create_ssl_context()


async def _handshake(host: str, ip: str, port: int) -> None:
    """A TLS handshake with ip:port as `host`; it raises what went wrong. The connection is closed unused."""
    _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port, ssl=_context(), server_hostname=host), WAIT)
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 2)
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        pass


HANDSHAKE = _handshake       # the real one, for a test that wants it back: the tests put a stand-in in its place


async def check(host: str, ip: str, port: int) -> None:
    """Raise ToolError unless the site at ip:port shows a certificate that is valid for `host`."""
    key = (host.lower().rstrip("."), ip, port)
    if _good.get(key, 0.0) > _now():
        return
    try:
        await _handshake(key[0], ip, port)
    except ssl.SSLCertVerificationError as e:
        why = (e.verify_message or str(e)).rstrip(".")
        raise ToolError(f"the certificate of {key[0]} is not valid ({why}). Nothing was sent. {HOW_ELSE}") from None
    except (ssl.SSLError, OSError, asyncio.TimeoutError) as e:
        why = ("no answer in time" if isinstance(e, asyncio.TimeoutError) else
               "nothing answers there" if isinstance(e, ConnectionRefusedError) else
               "what answers there does not speak TLS as expected" if isinstance(e, ssl.SSLError) else type(e).__name__)
        raise ToolError(f"the certificate of {key[0]} could not be checked ({why}), and what cannot be checked is not taken "
                        f"for good. Nothing was sent. {HOW_ELSE}") from None
    _good[key] = _now() + KEEP
