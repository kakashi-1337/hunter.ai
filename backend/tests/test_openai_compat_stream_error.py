import json

import httpx
import pytest
import respx

from hunterai.providers import openai_compat
from hunterai.providers.base import Options, ProviderError, TextDelta, WireMessage
from hunterai.types import TextBlock

URL = "https://api.abliteration.ai/v1/chat/completions"


def sse(*chunks):
    lines = []
    for c in chunks:
        lines += ["data: " + json.dumps(c), ""]
    lines += ["data: [DONE]", ""]
    return "\n".join(lines)


@pytest.fixture
def ablit(monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "ak_test")
    return openai_compat.abliteration()


def stream(p):
    return p.stream("abliterated-model", "sys",
                    [WireMessage(role="user", blocks=[TextBlock(text="hi")])], Options())


@pytest.mark.parametrize("error,kind", [
    ({"message": "boom", "code": 401}, "auth"),
    ({"message": "boom", "code": "402"}, "credits"),
    ({"message": "boom", "code": 403}, "policy"),
    ({"message": "boom", "code": 429}, "rate_limit"),
    ({"message": "boom", "code": 503}, "unavailable"),
    ({"message": "boom", "code": 400}, "bad_request"),
    ({"message": "Incorrect API key", "type": "invalid_request_error",
      "code": "invalid_api_key"}, "auth"),
    ({"message": "You exceeded your quota", "type": "insufficient_quota",
      "code": "insufficient_quota"}, "credits"),
    ({"message": "slow down", "type": "rate_limit_error"}, "rate_limit"),
    ({"message": "blocked", "code": "content_filter"}, "policy"),
    ({"message": "bad param", "type": "invalid_request_error"}, "bad_request"),
    ({"message": "The server is overloaded", "type": "server_error"}, "unavailable"),
    ({"message": "Rate limit reached for requests"}, "rate_limit"),
    ({"message": "something odd happened"}, "unavailable"),
    ("plain string failure", "unavailable"),
])
@respx.mock
async def test_error_chunk_in_200_stream_raises_with_kind(ablit, error, kind):
    respx.post(URL).mock(return_value=httpx.Response(200, text=sse({"error": error})))
    with pytest.raises(ProviderError) as ei:
        [e async for e in stream(ablit)]
    assert ei.value.kind == kind
    text = error if isinstance(error, str) else error["message"]
    assert text in ei.value.message


@respx.mock
async def test_unclassified_error_chunk_before_output_is_fallback_eligible(ablit):
    respx.post(URL).mock(return_value=httpx.Response(
        200, text=sse({"error": {"message": "upstream exploded"}})))
    events = []
    with pytest.raises(ProviderError) as ei:
        async for e in stream(ablit):
            events.append(e)
    assert events == [] and ei.value.kind == "unavailable" and ei.value.fallback_eligible


@respx.mock
async def test_error_chunk_after_partial_text_raises_after_the_text(ablit):
    respx.post(URL).mock(return_value=httpx.Response(200, text=sse(
        {"choices": [{"delta": {"content": "par"}}]},
        {"error": {"message": "died mid-stream", "code": 502}})))
    events = []
    with pytest.raises(ProviderError) as ei:
        async for e in stream(ablit):
            events.append(e)
    assert events == [TextDelta("par")]
    assert ei.value.kind == "unavailable" and "died mid-stream" in ei.value.message


@respx.mock
async def test_error_chunk_message_never_contains_key(ablit):
    respx.post(URL).mock(return_value=httpx.Response(200, text=sse(
        {"error": {"message": "bad key ak_test", "code": "invalid_api_key"}})))
    with pytest.raises(ProviderError) as ei:
        [e async for e in stream(ablit)]
    assert ei.value.kind == "auth" and "ak_test" not in ei.value.message


@respx.mock
async def test_null_error_field_is_not_an_error(ablit):
    respx.post(URL).mock(return_value=httpx.Response(200, text=sse(
        {"error": None, "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})))
    assert TextDelta("ok") in [e async for e in stream(ablit)]
