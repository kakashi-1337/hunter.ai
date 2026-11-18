import pytest

from hunterai.providers import registry
from hunterai.providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                                     Usage, WireMessage, error_from_status, render_file_block)
from hunterai.providers.mock import MockProvider
from hunterai.types import FileBlock, TextBlock


@pytest.mark.parametrize("status,kind", [
    (401, "auth"), (402, "credits"), (403, "policy"), (429, "rate_limit"),
    (400, "bad_request"), (404, "bad_request"), (422, "bad_request"),
    (500, "unavailable"), (502, "unavailable"), (503, "unavailable"), (529, "unavailable"),
])
def test_error_from_status_mapping(status, kind):
    e = error_from_status(status, "body", None)
    assert e.kind == kind and e.status == status


def test_error_retry_after_parsed_and_tolerant():
    assert error_from_status(429, "", "7").retry_after == 7.0
    assert error_from_status(429, "", "soon").retry_after is None
    assert error_from_status(429, "", None).retry_after is None


def test_error_message_extracted_from_json_body():
    e = error_from_status(402, '{"error": {"message": "Insufficient credits"}}', None)
    assert "Insufficient credits" in e.message


@pytest.mark.parametrize("kind,eligible", [
    ("unavailable", True), ("timeout", True), ("rate_limit", True),
    ("auth", False), ("credits", False), ("policy", False), ("bad_request", False),
])
def test_fallback_eligible(kind, eligible):
    assert ProviderError(kind, "x").fallback_eligible is eligible


def user(text):
    return [WireMessage(role="user", blocks=[TextBlock(text=text)])]


async def collect(p, text):
    return [e async for e in p.stream("mock-1", "sys", user(text), Options())]


async def test_mock_echo_sequence():
    assert await collect(MockProvider(), "hi") == [
        ThinkingDelta("thinking"), TextDelta("echo: hi"), Usage(3, 5), Stop("end")]


async def test_mock_fail_before_output():
    with pytest.raises(ProviderError) as ei:
        await collect(MockProvider(), "fail:credits")
    assert ei.value.kind == "credits"


async def test_mock_partial_fail():
    got = []
    with pytest.raises(ProviderError) as ei:
        async for e in MockProvider().stream("mock-1", "", user("partial-fail"), Options()):
            got.append(e)
    assert got == [TextDelta("half")] and ei.value.kind == "unavailable"


async def test_mock_slow_yields_five_deltas():
    evs = await collect(MockProvider(), "slow")
    assert len([e for e in evs if isinstance(e, TextDelta)]) == 5
    assert isinstance(evs[-2], Usage) and isinstance(evs[-1], Stop)


def test_registry_hides_mock_by_default(monkeypatch):
    assert "mock" not in [p.name for p in registry.all()]
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    assert "mock" in [p.name for p in registry.all()]
    assert registry.get("mock").name == "mock"


def test_registry_unknown_raises():
    with pytest.raises(KeyError):
        registry.get("nope")


def test_registry_order_is_stable():
    order = ["abliteration", "anthropic", "openai", "google", "xai", "ollama", "colibri", "codex", "claude_code"]
    names = [p.name for p in registry.all()]
    assert names == [n for n in order if n in names]


def test_options_reject_unknown_effort():
    with pytest.raises(Exception):
        Options(effort="ludicrous")


def test_render_file_block_text_and_binary():
    t = render_file_block(FileBlock(upload_id="u", filename="a.txt", text="hello", truncated=True))
    assert "a.txt" in t and "hello" in t and "truncated" in t
    b = render_file_block(FileBlock(upload_id="u", filename="x.bin", text=None, size=12))
    assert b == "[attached file: x.bin, 12 bytes, binary, not readable in this phase]"
