import json

import httpx
import pytest
import respx

from hunterai import db, notify
from hunterai.providers.base import ToolCall
from hunterai.repo import findings, settings, targets
from hunterai.tools import mind, registry
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)
from tests.toolkit import run_call, seeded_run

HOOK = "https://discord.com/api/webhooks/123456789012345678/abcDEF_123-xyz"


@pytest.fixture
def hooked(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)


async def finding(conn, title="IDOR on /orders", severity="high", triage=None, status=None):
    target = await targets.create(conn, "acme")
    f = await findings.add(conn, target.id, title, severity, "GET /orders/2 -> 200, secret token abc", triage=triage or {"false_positive": 0.1})
    if status:
        f = await findings.update(conn, f.id, status=status)
    return f, target


def test_only_a_discord_webhook_address_is_taken(monkeypatch):
    for bad in ("", "http://discord.com/api/webhooks/1/a", "https://evil.test/api/webhooks/1/a", "https://discord.com.evil.test/api/webhooks/1/a",
                "https://discord.com/api/webhooks/1/a?wait=true#x", "https://discord.com/api/users/1"):
        assert notify.valid(bad) is False
    for good in (HOOK, "https://discordapp.com/api/webhooks/1/a-b_c", "https://ptb.discord.com/api/webhooks/9/Z"):
        assert notify.valid(good) is True


def test_the_address_is_read_from_either_name_when_it_is_a_webhook(monkeypatch):
    assert notify.webhook() is None
    bot_token = ".".join(["M" * 24, "Gabcde", "x" * 38])            # shaped like a bot token, built here so that no token-like text is in the file
    monkeypatch.setenv("DISCORD_TOKEN", bot_token)                    # a bot token: not usable here
    assert notify.webhook() is None
    monkeypatch.setenv("DISCORD_TOKEN", HOOK)
    assert notify.webhook() == HOOK
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK + "2")
    assert notify.webhook() == HOOK + "2"                              # the proper name wins


def test_what_counts_as_a_positive_finding():
    f = findings.Finding(id="1", target_id="t", title="x", severity="high", status="open", evidence="", created_at="t")
    assert notify.positive(f.model_copy(update={"triage": {"false_positive": 0.1}})) is True
    assert notify.positive(f.model_copy(update={"triage": {"false_positive": 0.7}})) is False        # the triage doubts it
    assert notify.positive(f.model_copy(update={"triage": {"false_positive": 0.1, "invalid": True}})) is False
    assert notify.positive(f) is False                                                               # never triaged: not known to be real
    assert notify.positive(f.model_copy(update={"status": "confirmed"})) is True                     # the owner or the agent confirmed it
    for status in ("false_positive", "duplicate", "fixed", "reported"):
        assert notify.positive(f.model_copy(update={"status": status, "triage": {"false_positive": 0.0}})) is False


@respx.mock
async def test_a_positive_finding_is_sent_once_without_its_evidence(conn, hooked):
    sent = respx.post(HOOK).mock(return_value=httpx.Response(204))
    f, target = await finding(conn, title="IDOR @everyone on /orders")
    assert await notify.finding(conn, f.id) == "sent"
    body = json.loads(sent.calls.last.request.content)
    text = json.dumps(body)
    assert body["allowed_mentions"] == {"parse": []} and "IDOR @everyone on /orders" in text and "acme" in text and "HIGH" in text
    assert "secret token abc" not in text and "GET /orders/2" not in text                # evidence never leaves this machine this way
    assert await notify.finding(conn, f.id) == "already sent" and sent.call_count == 1
    assert (await findings.get(conn, f.id)).notified_at


@respx.mock
async def test_nothing_is_sent_for_a_doubtful_or_minor_finding_or_when_switched_off(conn, hooked):
    sent = respx.post(HOOK).mock(return_value=httpx.Response(204))
    doubtful, _ = await finding(conn, triage={"false_positive": 0.8})
    assert await notify.finding(conn, doubtful.id) == "not a positive finding"
    info, _ = await finding(conn, severity="info")
    assert await notify.finding(conn, info.id) == "below the severity you set (low)"
    await settings.put(conn, "notify_min_severity", "high")
    medium, _ = await finding(conn, severity="medium")
    assert await notify.finding(conn, medium.id) == "below the severity you set (high)"
    real, _ = await finding(conn)
    await settings.put(conn, "notify_findings", False)
    assert await notify.finding(conn, real.id) == "notifications are switched off"
    assert sent.call_count == 0
    # Later confirmed by the owner: now it is positive, and it goes out.
    await settings.put(conn, "notify_findings", True)
    await findings.update(conn, doubtful.id, status="confirmed", severity="high")
    assert await notify.finding(conn, doubtful.id) == "sent" and sent.call_count == 1


@respx.mock
async def test_a_discord_that_fails_never_breaks_anything(conn, hooked, monkeypatch):
    f, _ = await finding(conn)
    respx.post(HOOK).mock(return_value=httpx.Response(429, json={"message": "rate limited"}))
    assert (await notify.finding(conn, f.id)).startswith("Discord answered HTTP 429")
    assert (await findings.get(conn, f.id)).notified_at is None                          # it can be tried again
    respx.post(HOOK).mock(side_effect=httpx.ConnectError("down"))
    assert "could not reach Discord" in await notify.finding(conn, f.id)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://evil.test/hook")
    assert await notify.finding(conn, f.id) == "no Discord webhook is set"
    assert await notify.finding(conn, "nope") == "no such finding"


@respx.mock
async def test_recording_and_confirming_a_finding_notifies(client, conn, hooked, monkeypatch):
    sent = respx.post(HOOK).mock(return_value=httpx.Response(204))

    async def triage(conn_, target, title, evidence, known):
        return {"severity": "high", "false_positive": 0.9 if "maybe" in title else 0.1}
    monkeypatch.setattr(mind, "triage", triage)
    for t in mind.tools():
        if registry.get(t.name) is None:
            registry.register(t)
    chat, run_id = await seeded_run(conn, scope={"in": ["acme.test"]}, permissions={"risk": {"read": "allow", "write": "allow"}})
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", "finding_add", {"title": "SQL injection in search", "evidence": "proof"}))
    assert not out.block.is_error and sent.call_count == 1
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c2", "finding_add", {"title": "maybe an open redirect", "evidence": "?"}))
    assert sent.call_count == 1                                                          # doubted by the triage: silent
    fid = json.loads(out.block.content)["id"]
    r = await client.patch(f"/api/findings/{fid}", json={"status": "confirmed"})          # the owner confirms it in the interface
    assert r.status_code == 200 and sent.call_count == 2
    r = await client.post("/api/notify/test")
    assert r.json() == {"ok": True, "detail": "sent"} and sent.call_count == 3
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    assert (await client.post("/api/notify/test")).json() == {"ok": False, "detail": "no Discord webhook is set"}
