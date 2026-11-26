"""The sampling options of a chat on a local model: top_k, min_p, the repeat penalty and how far it looks back, the
presence and frequency penalties, and the seed. Ollama takes them under "options", llama-server as fields of the
request (both were seen to: see the README). A provider that was not seen to take them is sent none of them."""
import json

import httpx
import pytest
import respx

from hunterai.providers import colibri, llamacpp, ollama, openai_compat
from hunterai.providers.base import SAMPLING, Options, WireMessage
from hunterai.types import TextBlock
from tests import colibri_wire
from tests import llamacpp_wire as wire
from tests.test_ollama import BODY, show

ALL = {"top_k": 20, "min_p": 0.1, "repeat_penalty": 1.15, "repeat_last_n": 128, "presence_penalty": 0.5, "frequency_penalty": 0.25,
       "seed": 42}
# Set, though each counts as nothing or as "off": what is set is sent, whatever its value.
ZERO = {"top_k": 0, "min_p": 0.0, "repeat_last_n": 0, "presence_penalty": 0.0, "frequency_penalty": 0.0, "seed": 0}
ASK = [WireMessage("user", [TextBlock(text="hi")])]
OLLAMA = "http://127.0.0.1:11434"
LLAMA = "http://127.0.0.1:9931"
QWEN = "qwen3.5-0.8b"


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def test_the_sampling_options_are_these_seven_and_a_chat_can_carry_each():
    assert SAMPLING == ["top_k", "min_p", "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty", "seed"]
    assert set(ALL) == set(SAMPLING)
    kept = Options(**ALL).model_dump()
    assert {name: kept[name] for name in SAMPLING} == ALL
    assert all(value is None for name, value in Options().model_dump().items() if name in SAMPLING)


def test_a_model_that_does_not_take_them_has_them_cleared():
    left = Options(**ALL, temperature=0.2).only(["temperature", "top_k"]).model_dump()
    assert left["temperature"] == 0.2 and left["top_k"] == 20
    assert all(left[name] is None for name in SAMPLING if name != "top_k")


# ---- Ollama

async def ask_ollama(options: Options) -> dict:
    respx.post(OLLAMA + "/api/show").mock(return_value=show())
    route = respx.post(OLLAMA + "/api/chat").mock(return_value=httpx.Response(200, text=BODY))
    [e async for e in ollama.make().stream("qwen3:8b", "sys", ASK, options)]
    return sent(route)["options"]


@respx.mock
async def test_ollama_gets_what_is_set_under_its_options():
    assert await ask_ollama(Options(**ALL)) == {"num_ctx": ollama.DEFAULT_NUM_CTX, **ALL}


@respx.mock
async def test_ollama_gets_only_what_is_set():
    assert await ask_ollama(Options(top_k=40, temperature=0.3)) == {"num_ctx": ollama.DEFAULT_NUM_CTX, "top_k": 40, "temperature": 0.3}
    assert await ask_ollama(Options()) == {"num_ctx": ollama.DEFAULT_NUM_CTX}


@respx.mock
async def test_ollama_gets_a_value_that_is_set_to_nothing():
    assert await ask_ollama(Options(**ZERO)) == {"num_ctx": ollama.DEFAULT_NUM_CTX, **ZERO}


@respx.mock
async def test_every_ollama_model_offers_them_whether_it_thinks_or_not():
    respx.get(OLLAMA + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.2:3b"}]}))
    answers = {b"qwen3": show({"values": [False, True], "default": True}), b"llama3.2": show({"values": [False], "default": False})}
    respx.post(OLLAMA + "/api/show").mock(side_effect=lambda req: next(v for k, v in answers.items() if k in req.content))
    by = {m.id: m.options for m in await ollama.make().models()}
    assert by["qwen3:8b"] == ["temperature", "top_p", "max_tokens", "context_window", *SAMPLING, "thinking"]
    assert by["llama3.2:3b"] == ["temperature", "top_p", "max_tokens", "context_window", *SAMPLING]


# ---- llama.cpp

async def ask_llama(options: Options, monkeypatch) -> dict:
    monkeypatch.setenv(llamacpp.URL_ENV, LLAMA)
    respx.get(LLAMA + "/v1/models").mock(return_value=httpx.Response(200, text=wire.MODELS_QWEN))
    route = respx.post(LLAMA + "/v1/chat/completions").mock(return_value=httpx.Response(200, text=wire.PLAIN))
    [e async for e in llamacpp.make().stream(QWEN, "sys", ASK, options)]
    return sent(route)


@respx.mock
async def test_llama_server_gets_what_is_set_as_fields_of_the_request(monkeypatch):
    body = await ask_llama(Options(**ALL), monkeypatch)
    assert {name: body[name] for name in SAMPLING} == ALL


@respx.mock
async def test_llama_server_gets_only_what_is_set(monkeypatch):
    body = await ask_llama(Options(repeat_penalty=1.3), monkeypatch)
    assert body["repeat_penalty"] == 1.3 and set(SAMPLING) & set(body) == {"repeat_penalty"}
    assert not set(SAMPLING) & set(await ask_llama(Options(), monkeypatch))


@respx.mock
async def test_llama_server_gets_a_value_that_is_set_to_nothing(monkeypatch):
    body = await ask_llama(Options(**ZERO), monkeypatch)
    assert {name: body[name] for name in ZERO} == ZERO and "repeat_penalty" not in body


@respx.mock
async def test_a_model_of_llama_server_offers_them(monkeypatch):
    monkeypatch.setenv(llamacpp.URL_ENV, LLAMA)
    respx.get(LLAMA + "/v1/models").mock(return_value=httpx.Response(200, text=wire.MODELS_QWEN))
    (model,) = await llamacpp.make().models()
    assert model.options == ["temperature", "top_p", "max_tokens", *SAMPLING, "thinking"]


# ---- the providers that were not seen to take them

@respx.mock
async def test_a_cloud_provider_with_the_same_kind_of_api_is_sent_none_of_them(monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "ak_test")
    route = respx.post("https://api.abliteration.ai/v1/chat/completions").mock(return_value=httpx.Response(200, text=wire.PLAIN))
    [e async for e in openai_compat.abliteration().stream("abliterated-model", "sys", ASK, Options(**ALL, temperature=0.2))]
    body = sent(route)
    assert body["temperature"] == 0.2 and not set(SAMPLING) & set(body)


@respx.mock
async def test_colibri_is_sent_none_of_them(monkeypatch):
    at = "http://127.0.0.1:8000"
    monkeypatch.setenv(colibri.URL_ENV, at)
    respx.get(at + "/v1/models").mock(return_value=httpx.Response(200, text=colibri_wire.MODELS_QWEN))
    route = respx.post(at + "/v1/chat/completions").mock(return_value=httpx.Response(200, text=colibri_wire.HELLO))
    [e async for e in colibri.make().stream("qwen3.6-colibri", "sys", ASK, Options(**ALL, temperature=0.2))]
    assert sent(route)["temperature"] == 0.2 and not set(SAMPLING) & set(sent(route))


@pytest.mark.parametrize("make", [openai_compat.abliteration, openai_compat.openai, openai_compat.xai, colibri.make])
def test_no_other_provider_offers_them_for_a_model_it_does_not_know(make):
    assert not set(SAMPLING) & set(make()._info("some-model").options)
