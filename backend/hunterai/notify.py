"""A message to the owner's Discord channel when a finding is positive: one that the triage
does not doubt, or one that was set to "confirmed". Nothing else is announced.

The address is a Discord webhook the owner puts in `.env` as DISCORD_WEBHOOK_URL (it is a
secret: whoever has it can post to that channel). What is sent: the finding's title,
severity, status, the target's name, and the triage figures. The evidence is never sent:
it can hold tokens and personal data, and Discord is someone else's server. A message is
sent once per finding. Whatever goes wrong here is returned as a sentence, never raised:
recording a finding must not fail because a chat service is down."""
from __future__ import annotations

import re

import httpx

from . import config
from .db import now
from .repo import findings, settings, targets

KEY = "DISCORD_WEBHOOK_URL"
ALSO = ("DISCORD_TOKEN",)        # read as well, when it holds a webhook address
_HOOK = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{1,25}/[A-Za-z0-9_-]{1,200}$")
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DEFAULT_ON = True
DEFAULT_MIN = "low"
COLOURS = {"info": 0x8A93A3, "low": 0x6CB6FF, "medium": 0xE6B450, "high": 0xF07178, "critical": 0xB3001B}
DOUBT = 0.5                  # a false-positive probability at or over this is not a positive finding


def valid(url: str | None) -> bool:
    return bool(url) and bool(_HOOK.match(url))


def webhook() -> str | None:
    """The webhook address from `.env`: DISCORD_WEBHOOK_URL, or DISCORD_TOKEN when that holds one
    (a name people use for it). Anything that is not a Discord webhook address is ignored."""
    for name in (KEY, *ALSO):
        url = (config.env(name) or "").strip()
        if valid(url):
            return url
    return None


def positive(f) -> bool:
    """Confirmed, or open with a triage that does not doubt it. A finding nobody judged is not announced."""
    if f.status == "confirmed":
        return True
    if f.status != "open":
        return False
    t = f.triage or {}
    p = t.get("false_positive")
    return isinstance(p, (int, float)) and not t.get("invalid") and p < DOUBT


async def _post(url: str, payload: dict) -> str:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
            r = await client.post(url, json={**payload, "allowed_mentions": {"parse": []}})     # a title cannot ping anyone
    except httpx.HTTPError as e:
        return f"could not reach Discord ({type(e).__name__})"
    if r.status_code not in (200, 204):
        return f"Discord answered HTTP {r.status_code}"
    return "sent"


async def finding(conn, finding_id: str) -> str:
    """Announce this finding if it is positive and was not announced yet. Returns what happened."""
    try:
        f = await findings.get(conn, finding_id)
        if f is None:
            return "no such finding"
        url = webhook()
        if url is None:
            return "no Discord webhook is set"
        if not await settings.get(conn, "notify_findings", DEFAULT_ON):
            return "notifications are switched off"
        if f.notified_at:
            return "already sent"
        if not positive(f):
            return "not a positive finding"
        least = await settings.get(conn, "notify_min_severity", DEFAULT_MIN)
        least = least if least in findings.SEVERITIES else DEFAULT_MIN
        if findings.SEVERITIES.index(f.severity if f.severity in findings.SEVERITIES else "info") < findings.SEVERITIES.index(least):
            return f"below the severity you set ({least})"
        target = await targets.get(conn, f.target_id)
        t = f.triage or {}
        facts = [f"Target: {target.name if target else '?'}", f"Status: {f.status.replace('_', ' ')}"]
        if isinstance(t.get("false_positive"), (int, float)):
            facts.append(f"Triage: false positive {t['false_positive']:.2f}"
                         + (f", in scope {t['in_scope']:.2f}" if isinstance(t.get("in_scope"), (int, float)) else ""))
        said = await _post(url, {"embeds": [{
            "title": f"[{f.severity.upper()}] {f.title}"[:250], "description": "\n".join(facts)[:1000],
            "color": COLOURS.get(f.severity, COLOURS["info"]),
            "footer": {"text": "HunterAI · the evidence stays on your machine"}}]})
        if said == "sent":
            await conn.execute("update findings set notified_at=? where id=?", (now(), f.id))
            await conn.commit()
        return said
    except Exception as e:  # noqa: BLE001 - never the caller's problem
        return f"notification failed ({type(e).__name__})"


async def test_message() -> str:
    url = webhook()
    if url is None:
        return "no Discord webhook is set"
    return await _post(url, {"content": "HunterAI can reach this channel. Positive findings will be announced here."})
