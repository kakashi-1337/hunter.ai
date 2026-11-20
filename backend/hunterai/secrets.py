"""Keep HunterAI's own key values, and the login of a proxy the owner set, out of anything stored,
published, or sent to a tool."""
from __future__ import annotations

from . import config

KEY_NAMES = ("ABLIT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
             "GEMINI_API_KEY")
MIN_LEN = 8          # shorter values would redact ordinary words
REDACTED = "[redacted]"


def values() -> list[str]:
    found = (config.env(n) for n in KEY_NAMES)
    return sorted({v for v in found if v and len(v) >= MIN_LEN}, key=len, reverse=True)


def _proxy_logins() -> list[tuple[str, str]]:
    """(what to find, what to leave in its place) for the login of every proxy the owner set. A password is
    masked only where it stands as a login: after its user name in an address, as typed or as stored, and in
    the value an HTTP proxy is given. A password alone may be an everyday word that pages and commands are
    full of."""
    import base64
    from urllib.parse import quote

    from . import upstream
    out = []
    for user, password in upstream.logins():
        for u, p in {(user, password), (quote(user, safe=""), quote(password, safe=""))}:
            out.append((f"{u}:{p}@", f"{u}:{REDACTED}@"))
        out.append((base64.b64encode(f"{user}:{password}".encode()).decode(), REDACTED))
    return out


def scrub(text: str) -> str:
    for v in values():
        text = text.replace(v, REDACTED)
    for found, left in _proxy_logins():
        text = text.replace(found, left)
    return text


def scrub_obj(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub_obj(k): scrub_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_obj(v) for v in value]
    return value
