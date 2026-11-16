import asyncio
import fcntl
import hashlib
import json
import math
import os
import time

import httpx
import pytest
import respx

from hunterai import config
from hunterai.kb import embed
from hunterai.kb.embed import EmbedError

URL = "http://127.0.0.1:11434/api/embed"
UNLOAD = "http://127.0.0.1:11434/api/generate"


@respx.mock
async def test_request_shape_and_vectors():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"embeddings": [[0.1] * 1024, [0.2] * 1024]}))
    out = await embed.get("ollama:bge-m3").embed(["a", "b"])
    assert json.loads(route.calls.last.request.content) == {"model": "bge-m3", "input": ["a", "b"], "truncate": False, "keep_alive": 0,
                                                           "options": {"num_ctx": 8192}}
    assert len(out) == 2 and len(out[0]) == 1024


@respx.mock
@pytest.mark.parametrize("setting,sent", [(None, 0), ("0", 0), ("90", 90), ("junk", 0), ("-5", 0), ("999999", 3600)])
async def test_the_model_is_unloaded_after_its_answer_unless_the_owner_says_how_long_it_stays(monkeypatch, setting, sent):
    """Left loaded (Ollama's own five minutes), it sat in memory beside a local chat model on every message."""
    monkeypatch.delenv("HUNTERAI_EMBED_KEEP_LOADED", raising=False)
    if setting is not None:
        monkeypatch.setenv("HUNTERAI_EMBED_KEEP_LOADED", setting)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"embeddings": [[0.1]]}))
    await embed.get("ollama:bge-m3").embed(["what the owner asked"])
    assert json.loads(route.calls.last.request.content)["keep_alive"] == sent


@respx.mock
async def test_a_job_keeps_the_model_between_its_batches_and_unloads_it_at_the_end(monkeypatch):
    monkeypatch.delenv("HUNTERAI_EMBED_KEEP_LOADED", raising=False)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"embeddings": [[0.1]]}))
    unload = respx.post(UNLOAD).mock(return_value=httpx.Response(200, json={"done": True, "done_reason": "unload"}))
    job = embed.get("ollama:bge-m3", stays_loaded=True)
    await job.embed(["a chunk"])
    assert json.loads(route.calls.last.request.content)["keep_alive"] == embed.JOB_KEEP_LOADED >= 60 and not unload.called
    await job.release()
    assert json.loads(unload.calls.last.request.content) == {"model": "bge-m3", "keep_alive": 0} and unload.call_count == 1


@respx.mock
async def test_unloading_a_model_that_is_gone_or_cannot_be_reached_is_not_an_error():
    respx.post(UNLOAD).mock(side_effect=httpx.ConnectError("refused"))
    await embed.get("ollama:bge-m3").release()
    respx.post(UNLOAD).mock(return_value=httpx.Response(404, json={"error": "model 'bge-m3' not found"}))
    await embed.get("ollama:bge-m3").release()
    await embed.get("fake:8").release()                       # the stand-in has nothing to unload


@respx.mock
async def test_dimension_is_read_from_the_response():
    respx.post(URL).mock(return_value=httpx.Response(200, json={"embeddings": [[0.1] * 768]}))
    assert await embed.dimension(embed.get("ollama:nomic-embed-text")) == 768


@respx.mock
async def test_embed_calls_never_overlap():
    live = {"now": 0, "peak": 0}

    async def handler(request):
        live["now"] += 1; live["peak"] = max(live["peak"], live["now"])
        await asyncio.sleep(0.05)
        live["now"] -= 1
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]]})
    respx.post(URL).mock(side_effect=handler)
    a, b = embed.get("ollama:bge-m3"), embed.get("ollama:bge-m3")
    await asyncio.gather(a.embed(["x"]), b.embed(["y"]), a.embed(["z"]))
    assert live["peak"] == 1


@respx.mock
async def test_lock_held_elsewhere_times_out_without_a_request():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"embeddings": [[0.1]]}))
    config.home().mkdir(parents=True, exist_ok=True)
    fd = os.open(config.home() / "embed.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(EmbedError) as e:
            await embed.get("ollama:bge-m3", lock_timeout=0.2).embed(["x"])
    finally:
        os.close(fd)
    assert (e.value.kind, e.value.message) == ("unavailable", "embedder busy") and not route.called


@pytest.mark.parametrize("response,kind,fragment", [
    (httpx.ConnectError("x"), "unavailable", "not reachable"), (httpx.ReadTimeout("x"), "unavailable", "not reachable"),
    (httpx.Response(500), "unavailable", "500"), (httpx.Response(404), "bad_request", "ollama pull bge-m3"),
    (httpx.Response(400, json={"error": "input length exceeds context"}), "bad_request", "input length exceeds context"),
    (httpx.Response(200, json={"embeddings": [[0.1]]}), "bad_request", "wrong number"),
])
@respx.mock
async def test_errors(response, kind, fragment):
    respx.post(URL).mock(side_effect=response) if isinstance(response, Exception) else respx.post(URL).mock(return_value=response)
    with pytest.raises(EmbedError) as e:
        await embed.get("ollama:bge-m3").embed(["a", "b"])
    assert e.value.kind == kind and fragment in e.value.message


@respx.mock
async def test_empty_input_and_oversize_batch():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"embeddings": []}))
    assert await embed.get("ollama:bge-m3").embed([]) == [] and not route.called
    with pytest.raises(ValueError):
        await embed.get("ollama:bge-m3").embed(["x"] * 9)


async def test_fake_is_deterministic_and_rewards_word_overlap():
    e = embed.get("fake:64")
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731
    a, a2, b, c = await e.embed(["sqli payload union", "sqli payload union", "union sqli notes", "kernel heap spray"])
    assert a == a2 and len(a) == 64 and math.isclose(math.sqrt(dot(a, a)), 1.0)
    assert dot(a, b) > dot(a, c) and await embed.dimension(e) == 64


def test_fake_needs_the_flag_and_specs_are_validated(monkeypatch):
    for spec in ("openai:x", "ollama:", "ollama:a b", "bge-m3", "fake:"):
        with pytest.raises(ValueError):
            embed.get(spec)
    monkeypatch.delenv("HUNTERAI_FAKE_EMBED")
    with pytest.raises(ValueError):
        embed.get("fake:64")


async def test_fake_log_and_delay(monkeypatch, tmp_path):
    log = tmp_path / "embed.log"
    monkeypatch.setenv("HUNTERAI_FAKE_EMBED_LOG", str(log)); monkeypatch.setenv("HUNTERAI_FAKE_EMBED_DELAY", "0.05")
    began = time.monotonic()
    await embed.get("fake:8").embed(["a", "b"])
    assert time.monotonic() - began >= 0.05
    assert log.read_text().split() == [hashlib.sha256(b"a").hexdigest(), hashlib.sha256(b"b").hexdigest()]


async def test_unmocked_ollama_spec_fails_fast_on_a_closed_port(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:9")
    with pytest.raises(EmbedError) as e:
        await embed.get("ollama:bge-m3").embed(["x"])
    assert e.value.kind == "unavailable"
