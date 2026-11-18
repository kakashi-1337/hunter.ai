"""A site's certificate, checked by HunterAI itself.

With Caido in between, the tool that asks (web_fetch, web_download, caido_send, the browser) is shown Caido's
certificate, and Caido takes a site's certificate much as it comes: one that no authority signed, and one that
has expired, are passed on without a word (seen with a real Caido against badssl.com: both sites answered 200
through it). So before a request to an https site is handed to Caido, HunterAI makes a TLS
handshake with the site itself, at the address it checked, and goes by that. Nothing is sent in it. A
certificate that does not verify stops the call, as it would without Caido, and so does a site whose
certificate cannot be seen: what could not be checked is not taken for good.

The two are told apart in what the call is answered with. A certificate that was seen and is not valid is a
fact about the site. One that could not be seen is not: the connection may only have been cut, so a handshake
that the other side ends before it showed anything is made once more, and a call that is stopped for it says
to send it again, not how to get round the check.

A certificate found good is not asked for again for KEEP seconds; a bad one is asked for every time."""
from __future__ import annotations

import asyncio
import ssl
import time

from .tools.base import ToolError

KEEP = 600.0                 # seconds a certificate that was found good stays good
WAIT = 10.0                  # seconds for the handshake
AGAIN_AFTER = 0.5            # seconds before a handshake that was cut short is made once more
# The other side ended the connection before it showed a certificate. A plain close during the handshake is
# reported by the event loop as ConnectionResetError too (asyncio's and uvloop's alike), so the two are one case.
CUT = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, ssl.SSLEOFError, ssl.SSLZeroReturnError)

_good: dict[tuple[str, str, int], float] = {}
_now = time.monotonic
_pause = asyncio.sleep
NOT_VALID = ("Nothing was sent. While it records in Caido, HunterAI checks a site's certificate itself: Caido passes one "
             "like this on. To work with this site all the same, use curl -k in a command.")
NOT_SEEN = ("and what cannot be checked is not taken for good. Nothing was sent. This says nothing about the certificate "
            "itself and may pass: send the call again. If it stays like this, tell the owner; a command with curl -k "
            "reaches the site without this check.")


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


def _why(e: BaseException) -> str:
    """Why no certificate was seen, in words for the model and the owner."""
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        return "no answer in time"
    if isinstance(e, ConnectionRefusedError):
        return "nothing answers there"
    if isinstance(e, ssl.SSLError):      # the library's own name for it says the most: a tester reads it at a glance
        return "what answers there is not TLS, or not TLS this machine accepts" + (f": {e.reason}" if e.reason else "")
    return (e.strerror if isinstance(e, OSError) else None) or type(e).__name__


async def check(host: str, ip: str, port: int) -> None:
    """Raise ToolError unless the site at ip:port shows a certificate that is valid for `host`."""
    key = (host.lower().rstrip("."), ip, port)
    if _good.get(key, 0.0) > _now():
        return
    for attempt in (1, 2):
        try:
            await _handshake(key[0], ip, port)
            break
        except ssl.SSLCertVerificationError as e:
            why = (e.verify_message or str(e)).rstrip(".")
            raise ToolError(f"the certificate of {key[0]} is not valid ({why}). {NOT_VALID}") from None
        except CUT:
            if attempt == 1:             # a passing fault as often as not: once more, and only then is it said
                await _pause(AGAIN_AFTER)
                continue
            why = "the connection was closed twice before the site showed a certificate"
        except (ssl.SSLError, OSError, asyncio.TimeoutError) as e:
            why = _why(e)
        raise ToolError(f"the certificate of {key[0]} could not be checked ({why}), {NOT_SEEN}") from None
    _good[key] = _now() + KEEP
