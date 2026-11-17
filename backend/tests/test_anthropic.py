import base64
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest

from hunterai.providers.anthropic import AnthropicProvider, make
from hunterai.providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                                     Usage, WireMessage)
from hunterai.types import FileBlock, ImageBlock, TextBlock, ThinkingBlock


def delta(kind, **kw):
    return NS(type="content_block_delta", delta=NS(type=kind, **kw))


EVENTS = [NS(type="message_start"), delta("thinking_delta", thinking="hm"),
          delta("text_delta", text="Hel"), delta("text_delta", text="lo"),
          delta("signature_delta", signature="x"), NS(type="message_stop")]


class FakeStream:
    def __init__(self, events, final, error=None):
        self.events, self.final, self.error = events, final, error

    async def __aenter__(self):
        if self.error and not self.events:
            raise self.error
        return self

    async def __aexit__(self, *a):
        return False

    async def __aiter__(self):
        for e in self.events:
            yield e
        if self.error:
            raise self.error

    async def get_final_message(self):
        return self.final


class FakeMessages:
    def __init__(self, owner, label):
        self.owner, self.label = owner, label

    def stream(self, **kw):
        self.owner.calls.append((self.label, kw))
        return FakeStream(self.owner.events, self.owner.final, self.owner.error)


class FakeClient:
    def __init__(self, events=EVENTS, stop="end_turn", error=None, model_list=None):
        self.events, self.error, self.calls = events, error, []
        self.final = NS(stop_reason=stop, usage=NS(input_tokens=12, output_tokens=7))
        self.messages = FakeMessages(self, "stable")
        self.beta = NS(messages=FakeMessages(self, "beta"))
        self._models = model_list

    @property
    def models(self):
        owner = self

        class M:
            def list(self):
                if owner._models is None:
                    raise anthropic.APIConnectionError(request=httpx.Request("GET", "http://x"))

                async def gen():
                    for m in owner._models:
                        yield m
                return gen()
        return M()


def status_error(cls, status, headers=None):
    resp = httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "http://x"),
                          json={"error": {"message": "nope"}})
    return cls("nope", response=resp, body=None)


def user(*blocks, images=None):
    return [WireMessage("user", list(blocks), images or {})]


async def run(client, model="claude-opus-5-5", msgs=None, options=None, system="sys"):
    p = AnthropicProvider(client=client)
    return [e async for e in p.stream(model, system, msgs or user(TextBlock(text="hi")),
                                      options or Options())]


async def test_events_mapped():
    assert await run(FakeClient()) == [ThinkingDelta("hm"), TextDelta("Hel"), TextDelta("lo"),
                                       Usage(12, 7), Stop("end_turn")]


async def test_request_defaults_for_current_model():
    c = FakeClient()
    await run(c, options=Options(temperature=0.5, thinking=False, thinking_budget=2000))
    label, kw = c.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["system"] == "sys"
    assert kw["max_tokens"] == 64000
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in kw and "output_config" not in kw
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


async def test_effort_and_max_tokens_passed():
    c = FakeClient()
    await run(c, options=Options(effort="xhigh", max_tokens=1000))
    kw = c.calls[0][1]
    assert kw["output_config"] == {"effort": "xhigh"} and kw["max_tokens"] == 1000


async def test_refusal_fallback_enabled_on_models_that_support_it():
    c = FakeClient()
    await run(c, model="claude-opus-5-5")
    label, kw = c.calls[0]
    assert label == "beta" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]


async def test_other_models_use_stable_endpoint_without_fallbacks():
    c = FakeClient()
    await run(c, model="claude-haiku-5-5")
    label, kw = c.calls[0]
    assert label == "stable" and "fallbacks" not in kw and "betas" not in kw


async def test_legacy_model_gets_no_adaptive_thinking_or_effort():
    c = FakeClient()
    await run(c, model="claude-haiku-4-5", options=Options(effort="high"))
    kw = c.calls[0][1]
    assert "thinking" not in kw and "output_config" not in kw


async def test_no_system_key_when_empty():
    c = FakeClient()
    await run(c, system="")
    assert "system" not in c.calls[0][1]


async def test_images_files_and_history():
    c = FakeClient()
    msgs = [
        WireMessage("user", [TextBlock(text="look"), ImageBlock(upload_id="u", mime="image/png"),
                             FileBlock(upload_id="f", filename="a.txt", text="DATA")],
                    {"u": b"\x89PNG"}),
        WireMessage("assistant", [ThinkingBlock(text="private"), TextBlock(text="ans")]),
        WireMessage("user", [TextBlock(text="more")]),
    ]
    await run(c, msgs=msgs)
    m = c.calls[0][1]["messages"]
    assert m[0]["content"][0] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png",
        "data": base64.standard_b64encode(b"\x89PNG").decode()}}
    assert m[0]["content"][1] == {"type": "text", "text": "look"}
    assert "DATA" in m[0]["content"][2]["text"]
    assert m[1] == {"role": "assistant", "content": [{"type": "text", "text": "ans"}]}
    assert "private" not in str(m)


async def test_empty_messages_are_skipped():
    c = FakeClient()
    msgs = [WireMessage("user", [TextBlock(text="a")]), WireMessage("assistant", []),
            WireMessage("user", [TextBlock(text="b")])]
    await run(c, msgs=msgs)
    roles = [m["role"] for m in c.calls[0][1]["messages"]]
    assert roles == ["user", "user"]


async def test_refusal_without_text_raises_policy():
    with pytest.raises(ProviderError) as ei:
        await run(FakeClient(events=[], stop="refusal"))
    assert ei.value.kind == "policy"


async def test_refusal_after_text_ends_with_stop_refusal():
    evs = await run(FakeClient(stop="refusal"))
    assert evs[-1] == Stop("refusal")


@pytest.mark.parametrize("cls,status,kind", [
    (anthropic.AuthenticationError, 401, "auth"),
    (anthropic.PermissionDeniedError, 403, "policy"),
    (anthropic.RateLimitError, 429, "rate_limit"),
    (anthropic.BadRequestError, 400, "bad_request"),
    (anthropic.InternalServerError, 500, "unavailable"),
    (anthropic.APIStatusError, 402, "credits"),
])
async def test_status_errors_mapped(cls, status, kind):
    err = status_error(cls, status, {"retry-after": "7"})
    with pytest.raises(ProviderError) as ei:
        await run(FakeClient(events=[], error=err))
    assert ei.value.kind == kind and ei.value.status == status and ei.value.retry_after == 7.0


async def test_connection_and_timeout_errors():
    req = httpx.Request("POST", "http://x")
    with pytest.raises(ProviderError) as ei:
        await run(FakeClient(events=[], error=anthropic.APIConnectionError(request=req)))
    assert ei.value.kind == "unavailable"
    with pytest.raises(ProviderError) as ei:
        await run(FakeClient(events=[], error=anthropic.APITimeoutError(request=req)))
    assert ei.value.kind == "timeout"


async def test_error_mid_stream_after_text():
    err = status_error(anthropic.InternalServerError, 529)
    got = []
    p = AnthropicProvider(client=FakeClient(events=EVENTS[:3], error=err))
    with pytest.raises(ProviderError):
        async for e in p.stream("claude-opus-5-5", "", user(TextBlock(text="x")), Options()):
            got.append(e)
    assert got == [ThinkingDelta("hm"), TextDelta("Hel")]


async def test_models_from_api():
    c = FakeClient(model_list=[
        NS(id="claude-opus-5-5", display_name="Claude Opus 5.5", max_input_tokens=1000000),
        NS(id="claude-haiku-4-5", display_name="Claude Haiku 4.5", max_input_tokens=200000)])
    ms = await AnthropicProvider(client=c).models()
    assert [(m.id, m.label, m.context_window) for m in ms] == [
        ("claude-opus-5-5", "Claude Opus 5.5", 1000000)]            # the models before 4.6, with a 200K window, are not offered
    assert ms[0].options == ["max_tokens", "effort"] and ms[0].supports_images


async def test_models_static_fallback():
    ms = await AnthropicProvider(client=FakeClient(model_list=None)).models()
    ids = [m.id for m in ms]
    assert "claude-opus-5-5" in ids and "claude-fable-5-2" in ids and "claude-haiku-5-5" in ids


def test_configured_follows_key(monkeypatch):
    assert make().configured() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert make().configured() is True and make().name == "anthropic"


async def test_unconfigured_stream_is_auth_error():
    with pytest.raises(ProviderError) as ei:
        async for _ in make().stream("claude-opus-5-5", "", user(TextBlock(text="x")), Options()):
            pass
    assert ei.value.kind == "auth"
