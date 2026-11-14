import base64
import json

import httpx
import pytest
import respx

from hunterai.providers import openai_compat
from hunterai.providers.base import (IMAGE_OMITTED, Options, ProviderError, Stop, TextDelta,
                                     ThinkingDelta, Usage, WireMessage)
from hunterai.types import FileBlock, ImageBlock, TextBlock, ThinkingBlock

BASE = "https://api.abliteration.ai/v1"
URL = BASE + "/chat/completions"


def sse(*chunks):
    lines = [": keep-alive", ""]
    for c in chunks:
        lines += ["data: " + json.dumps(c), ""]
    lines += ["data: [DONE]", ""]
    return "\n".join(lines)


BODY = sse(
    {"choices": [{"delta": {"role": "assistant", "reasoning_content": "hmm"}}]},
    {"choices": [{"delta": {"content": "Hel"}}]},
    {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 4}},
)


@pytest.fixture
def ablit(monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "ak_test")
    return openai_compat.abliteration()


def user(*blocks, images=None):
    return [WireMessage(role="user", blocks=list(blocks), images=images or {})]


async def run(p, model="abliterated-model", msgs=None, options=None, system="sys"):
    return [e async for e in p.stream(model, system, msgs or user(TextBlock(text="hi")),
                                      options or Options())]


def sent(route):
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_stream_parses_reasoning_text_usage_stop(ablit):
    respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    assert await run(ablit) == [ThinkingDelta("hmm"), TextDelta("Hel"), TextDelta("lo"),
                                Usage(11, 4), Stop("stop")]


@respx.mock
async def test_reasoning_field_alias(ablit):
    body = sse({"choices": [{"delta": {"reasoning": "r"}}]},
               {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})
    respx.post(URL).mock(return_value=httpx.Response(200, text=body))
    evs = await run(ablit)
    assert evs[0] == ThinkingDelta("r")
    assert evs[-2:] == [Usage(0, 0), Stop("stop")]


@respx.mock
async def test_stream_without_finish_reason_still_ends_cleanly(ablit):
    body = sse({"choices": [{"delta": {"content": "x"}}]})
    respx.post(URL).mock(return_value=httpx.Response(200, text=body))
    assert (await run(ablit))[-2:] == [Usage(0, 0), Stop("end")]


@respx.mock
async def test_malformed_chunk_is_skipped(ablit):
    body = "data: {not json\n\n" + sse({"choices": [{"delta": {"content": "ok"}}]})
    respx.post(URL).mock(return_value=httpx.Response(200, text=body))
    assert TextDelta("ok") in await run(ablit)


@respx.mock
async def test_request_shape_and_auth(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(ablit, options=Options(effort="high", temperature=0.3, max_tokens=99,
                                     context_window=5, verbosity="low"))
    req = route.calls.last.request
    body = sent(route)
    assert req.headers["authorization"] == "Bearer ak_test"
    assert body["model"] == "abliterated-model" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["reasoning_effort"] == "high"
    assert body["temperature"] == 0.3 and body["max_tokens"] == 99
    assert "context_window" not in body and "verbosity" not in body and "num_ctx" not in body
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


@respx.mock
async def test_thinking_false_maps_to_effort_none(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(ablit, options=Options(thinking=False))
    assert sent(route)["reasoning_effort"] == "none"


@respx.mock
async def test_v2_cannot_disable_reasoning(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(ablit, model="abliterated-model-large-v2", options=Options(thinking=False))
    assert "reasoning_effort" not in sent(route)


@respx.mock
async def test_unknown_model_gets_basic_options_only(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(ablit, model="brand-new", options=Options(effort="high", temperature=1.0))
    body = sent(route)
    assert body["temperature"] == 1.0 and "reasoning_effort" not in body


@respx.mock
async def test_no_system_message_when_empty(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(ablit, system="")
    assert sent(route)["messages"][0]["role"] == "user"


@respx.mock
async def test_image_sent_as_data_url_when_supported(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    msgs = user(TextBlock(text="look"), ImageBlock(upload_id="u1", mime="image/png"),
                images={"u1": b"\x89PNG"})
    await run(ablit, msgs=msgs)
    content = sent(route)["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1] == {"type": "image_url", "image_url": {
        "url": "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()}}


@respx.mock
async def test_image_omitted_for_text_only_model(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    msgs = user(TextBlock(text="look"), ImageBlock(upload_id="u1", mime="image/png"),
                images={"u1": b"x"})
    await run(ablit, model="abliterated-model-large-v2", msgs=msgs)
    content = sent(route)["messages"][1]["content"]
    assert isinstance(content, str) and IMAGE_OMITTED in content and "look" in content


@respx.mock
async def test_files_inlined_and_thinking_not_replayed(ablit):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    msgs = [
        WireMessage("user", [TextBlock(text="q"),
                             FileBlock(upload_id="f", filename="a.txt", text="DATA")]),
        WireMessage("assistant", [ThinkingBlock(text="secret thoughts"), TextBlock(text="ans")]),
        WireMessage("user", [TextBlock(text="again")]),
    ]
    await run(ablit, msgs=msgs)
    m = sent(route)["messages"]
    assert "DATA" in m[1]["content"] and "a.txt" in m[1]["content"]
    assert m[2] == {"role": "assistant", "content": "ans"}
    assert "secret thoughts" not in json.dumps(m)


@pytest.mark.parametrize("status,kind", [(401, "auth"), (402, "credits"), (403, "policy"),
                                         (429, "rate_limit"), (503, "unavailable"),
                                         (400, "bad_request")])
@respx.mock
async def test_http_errors_map_to_kinds(ablit, status, kind):
    respx.post(URL).mock(return_value=httpx.Response(
        status, json={"error": {"message": "nope"}}, headers={"Retry-After": "7"}))
    with pytest.raises(ProviderError) as ei:
        await run(ablit)
    assert ei.value.kind == kind and ei.value.retry_after == 7.0 and "nope" in ei.value.message


@respx.mock
async def test_connect_error_is_unavailable(ablit):
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderError) as ei:
        await run(ablit)
    assert ei.value.kind == "unavailable"


@respx.mock
async def test_timeout_is_timeout(ablit):
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as ei:
        await run(ablit)
    assert ei.value.kind == "timeout"


@respx.mock
async def test_error_message_never_contains_key(ablit):
    respx.post(URL).mock(return_value=httpx.Response(401, text="bad key ak_test"))
    with pytest.raises(ProviderError) as ei:
        await run(ablit)
    assert "ak_test" not in ei.value.message


async def test_missing_key_is_auth_error_without_network(monkeypatch):
    p = openai_compat.abliteration()
    assert p.configured() is False
    with pytest.raises(ProviderError) as ei:
        await run(p)
    assert ei.value.kind == "auth"


@respx.mock
async def test_models_live_list_merged_with_capabilities(ablit):
    respx.get(BASE + "/models").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "abliterated-model"}, {"id": "brand-new"}]}))
    ms = {m.id: m for m in await ablit.models()}
    assert set(ms) == {"abliterated-model", "brand-new"}
    assert ms["abliterated-model"].supports_images and ms["abliterated-model"].context_window == 262144
    assert "effort" in ms["abliterated-model"].options
    assert ms["brand-new"].supports_images is False
    assert ms["brand-new"].options == ["temperature", "top_p", "max_tokens"]


@respx.mock
async def test_models_fall_back_to_defaults_on_failure(ablit):
    respx.get(BASE + "/models").mock(return_value=httpx.Response(500))
    ms = {m.id: m for m in await ablit.models()}
    assert set(ms) == {"abliterated-model", "abliterated-model-large-flash",
                       "abliterated-model-large-v2"}
    assert "thinking" in ms["abliterated-model"].options
    assert "thinking" not in ms["abliterated-model-large-v2"].options
    assert ms["abliterated-model-large-v2"].supports_images is False


@respx.mock
async def test_base_url_override(monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "k")
    monkeypatch.setenv("HUNTERAI_ABLIT_BASE_URL", "http://127.0.0.1:9/v1/")
    route = respx.post("http://127.0.0.1:9/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=BODY))
    await run(openai_compat.abliteration())
    assert route.called


def test_three_registrations(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "a")
    assert openai_compat.openai().configured() and openai_compat.openai().name == "openai"
    assert openai_compat.xai().configured() is False and openai_compat.xai().name == "xai"
    assert openai_compat.xai().base_url == "https://api.x.ai/v1"
