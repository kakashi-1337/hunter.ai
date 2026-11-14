from types import SimpleNamespace as NS

import pytest
from google.genai import errors

from hunterai.providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                                     Usage, WireMessage)
from hunterai.providers.google import GoogleProvider, make
from hunterai.types import FileBlock, ImageBlock, TextBlock, ThinkingBlock


def chunk(parts, usage=None, finish=None):
    cand = NS(content=NS(parts=[NS(text=t, thought=th) for t, th in parts]), finish_reason=finish)
    return NS(candidates=[cand], usage_metadata=usage)


CHUNKS = [
    chunk([("pondering", True)]),
    chunk([("Hel", None)]),
    chunk([("lo", False)], usage=NS(prompt_token_count=10, candidates_token_count=4,
                                   thoughts_token_count=6), finish=NS(name="STOP")),
]


class FakeClient:
    def __init__(self, chunks=CHUNKS, error=None, fail_after=None, model_list=None):
        self.chunks, self.error, self.fail_after = chunks, error, fail_after
        self.calls, self.model_list = [], model_list
        outer = self

        class Models:
            async def generate_content_stream(self, **kw):
                outer.calls.append(kw)
                if outer.error and outer.fail_after is None:
                    raise outer.error

                async def gen():
                    for i, c in enumerate(outer.chunks):
                        if outer.fail_after is not None and i == outer.fail_after:
                            raise outer.error
                        yield c
                return gen()

            async def list(self, **kw):
                if outer.model_list is None:
                    raise errors.ServerError(500, {"error": {"message": "down"}})

                async def gen():
                    for m in outer.model_list:
                        yield m
                return gen()
        self.aio = NS(models=Models())


def user(*blocks, images=None):
    return [WireMessage("user", list(blocks), images or {})]


async def run(client, options=None, msgs=None, system="sys", model="gemini-x"):
    p = GoogleProvider(client=client)
    return [e async for e in p.stream(model, system, msgs or user(TextBlock(text="hi")),
                                      options or Options())]


async def test_parts_usage_and_stop_mapped():
    assert await run(FakeClient()) == [ThinkingDelta("pondering"), TextDelta("Hel"),
                                       TextDelta("lo"), Usage(10, 10), Stop("STOP")]


async def test_request_shape():
    c = FakeClient()
    await run(c, Options(temperature=0.2, top_p=0.9, max_tokens=77, effort="high"))
    kw = c.calls[0]
    assert kw["model"] == "gemini-x"
    assert kw["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    cfg = kw["config"]
    assert cfg["system_instruction"] == "sys"
    assert (cfg["temperature"], cfg["top_p"], cfg["max_output_tokens"]) == (0.2, 0.9, 77)
    assert cfg["thinking_config"] == {"include_thoughts": True}
    assert "effort" not in str(cfg)


async def test_thinking_options():
    c = FakeClient()
    await run(c, Options(thinking_budget=2048))
    assert c.calls[0]["config"]["thinking_config"] == {"include_thoughts": True,
                                                       "thinking_budget": 2048}
    await run(c, Options(thinking=False, thinking_budget=2048))
    assert c.calls[1]["config"]["thinking_config"] == {"thinking_budget": 0}


async def test_roles_images_files_history():
    c = FakeClient()
    msgs = [
        WireMessage("user", [TextBlock(text="look"), ImageBlock(upload_id="u", mime="image/png"),
                             FileBlock(upload_id="f", filename="a.txt", text="DATA")],
                    {"u": b"PNG"}),
        WireMessage("assistant", [ThinkingBlock(text="private"), TextBlock(text="ans")]),
        WireMessage("assistant", []),
        WireMessage("user", [TextBlock(text="more")]),
    ]
    await run(c, msgs=msgs, system="")
    contents = c.calls[0]["contents"]
    assert [x["role"] for x in contents] == ["user", "model", "user"]
    assert contents[0]["parts"][0] == {"text": "look"}
    assert contents[0]["parts"][1] == {"inline_data": {"mime_type": "image/png", "data": b"PNG"}}
    assert "DATA" in contents[0]["parts"][2]["text"]
    assert contents[1]["parts"] == [{"text": "ans"}]
    assert "private" not in str(contents)
    assert "system_instruction" not in c.calls[0]["config"]


async def test_chunk_without_candidates_or_parts_is_tolerated():
    weird = [NS(candidates=None, usage_metadata=None),
             NS(candidates=[NS(content=None, finish_reason=None)], usage_metadata=None),
             chunk([("ok", None)])]
    evs = await run(FakeClient(chunks=weird))
    assert evs == [TextDelta("ok"), Usage(0, 0), Stop("end")]


@pytest.mark.parametrize("code,kind", [(400, "bad_request"), (401, "auth"), (403, "policy"),
                                       (429, "rate_limit"), (500, "unavailable"),
                                       (503, "unavailable")])
async def test_errors_mapped(code, kind):
    cls = errors.ServerError if code >= 500 else errors.ClientError
    err = cls(code, {"error": {"message": "nope"}})
    with pytest.raises(ProviderError) as ei:
        await run(FakeClient(error=err))
    assert ei.value.kind == kind and "nope" in ei.value.message


async def test_error_mid_stream():
    err = errors.ServerError(503, {"error": {"message": "x"}})
    got = []
    p = GoogleProvider(client=FakeClient(error=err, fail_after=1))
    with pytest.raises(ProviderError):
        async for e in p.stream("m", "", user(TextBlock(text="x")), Options()):
            got.append(e)
    assert got == [ThinkingDelta("pondering")]


async def test_models_filtered_to_generators():
    ml = [NS(name="models/gemini-pro-9", display_name="Gemini Pro 9", input_token_limit=1000,
             supported_actions=["generateContent", "countTokens"]),
          NS(name="models/text-embedding-9", display_name="Emb", input_token_limit=1,
             supported_actions=["embedContent"]),
          NS(name="models/imagen-9", display_name="Img", input_token_limit=1,
             supported_actions=None)]
    ms = await GoogleProvider(client=FakeClient(model_list=ml)).models()
    assert [(m.id, m.label, m.context_window) for m in ms] == [("gemini-pro-9", "Gemini Pro 9", 1000)]
    assert ms[0].supports_images
    assert ms[0].options == ["temperature", "top_p", "max_tokens", "thinking", "thinking_budget"]


async def test_models_empty_on_failure():
    assert await GoogleProvider(client=FakeClient(model_list=None)).models() == []


def test_configured_follows_key(monkeypatch):
    assert make().configured() is False
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert make().configured() is True and make().name == "google"


async def test_unconfigured_stream_is_auth_error():
    with pytest.raises(ProviderError) as ei:
        async for _ in make().stream("m", "", user(TextBlock(text="x")), Options()):
            pass
    assert ei.value.kind == "auth"
