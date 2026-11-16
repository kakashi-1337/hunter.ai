import json

import httpx
import pytest
import respx

from hunterai.providers import ollama
from hunterai.providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                                     Usage, WireMessage)
from hunterai.types import ImageBlock, TextBlock

HOST = "http://127.0.0.1:11434"
URL = HOST + "/api/chat"


def ndjson(*objs):
    return "\n".join(json.dumps(o) for o in objs) + "\n"


BODY = ndjson(
    {"message": {"role": "assistant", "content": "", "thinking": "hm"}, "done": False},
    {"message": {"role": "assistant", "content": "Hi"}, "done": False},
    {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
     "prompt_eval_count": 9, "eval_count": 2},
)


async def run(options=None, msgs=None, model="qwen3:8b", system="sys"):
    msgs = msgs or [WireMessage("user", [TextBlock(text="hi")])]
    return [e async for e in ollama.make().stream(model, system, msgs, options or Options())]


def sent(route):
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_ndjson_parsed_to_events():
    respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    assert await run() == [ThinkingDelta("hm"), TextDelta("Hi"), Usage(9, 2), Stop("stop")]


def show(thinking=None, capabilities=("completion",)):
    """What /api/show answers: `thinking` is the object newer Ollama versions report, None for an older one."""
    return httpx.Response(200, json={"capabilities": list(capabilities), **({} if thinking is None else {"thinking": thinking})})


LEVELS = {"values": ["low", "medium", "high"], "default": "medium"}
ON_OFF = {"values": [False, True], "default": True}


@respx.mock
async def test_options_mapped():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    respx.post(HOST + "/api/show").mock(return_value=show(ON_OFF))          # a model with a switch, no levels
    await run(Options(context_window=8192, max_tokens=50, temperature=0.1, top_p=0.9,
                      thinking=True, effort="high"))
    body = sent(route)
    assert body["options"] == {"num_ctx": 8192, "num_predict": 50, "temperature": 0.1,
                               "top_p": 0.9}
    assert body["think"] is True and body["stream"] is True
    assert "effort" not in json.dumps(body)
    assert body["messages"][0] == {"role": "system", "content": "sys"}


@respx.mock
async def test_think_absent_when_unset():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run()
    assert "think" not in sent(route) and sent(route)["options"] == {"num_ctx": 8192}


@respx.mock
async def test_image_becomes_placeholder_text():
    respx.post(HOST + "/api/show").mock(return_value=httpx.Response(200, json={"capabilities": ["completion"]}))
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(msgs=[WireMessage("user", [TextBlock(text="x"),
                                         ImageBlock(upload_id="u", mime="image/png")],
                                {"u": b"1"})])
    m = sent(route)["messages"][1]
    assert "image omitted" in m["content"] and "images" not in m


@respx.mock
async def test_models_filter_embedders():
    respx.get(HOST + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [
        {"name": "qwen3:8b"}, {"name": "bge-m3:latest"}, {"name": "nomic-embed-text:latest"},
        {"name": "llama3.1:latest"}]}))
    respx.post(HOST + "/api/show").mock(side_effect=lambda req: httpx.Response(200, json={
        "capabilities": ["completion", "vision"] if b"llama3.1" in req.content else ["completion"]}))
    ms = await ollama.make().models()
    assert [m.id for m in ms] == ["qwen3:8b", "llama3.1:latest"]
    assert ms[0].options == ["temperature", "top_p", "max_tokens", "context_window", "thinking"]
    assert ms[0].supports_images is False and ms[1].supports_images is True


@respx.mock
async def test_models_empty_when_ollama_down():
    respx.get(HOST + "/api/tags").mock(side_effect=httpx.ConnectError("x"))
    assert await ollama.make().models() == []


@respx.mock
async def test_connection_refused_is_unavailable():
    respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderError) as ei:
        await run()
    assert ei.value.kind == "unavailable"


@respx.mock
async def test_missing_model_names_pull_command():
    respx.post(URL).mock(return_value=httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ProviderError) as ei:
        await run(model="nope:1b")
    assert ei.value.kind == "bad_request" and "ollama pull nope:1b" in ei.value.message


@respx.mock
async def test_error_line_mid_stream_raises():
    respx.post(URL).mock(return_value=httpx.Response(200, text=ndjson(
        {"message": {"content": "a"}, "done": False}, {"error": "out of memory"})))
    got = []
    with pytest.raises(ProviderError) as ei:
        async for e in ollama.make().stream("m", "", [WireMessage("user", [TextBlock(text="x")])],
                                            Options()):
            got.append(e)
    assert got == [TextDelta("a")] and "out of memory" in ei.value.message


@respx.mock
async def test_host_from_env_normalized(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11500/")
    route = respx.post("http://127.0.0.1:11500/api/chat").mock(
        return_value=httpx.Response(200, text=BODY))
    await run()
    assert route.called


def test_always_configured():
    assert ollama.make().configured() is True and ollama.make().name == "ollama"


@respx.mock
async def test_image_sent_to_vision_model():
    import base64
    show = respx.post(HOST + "/api/show").mock(
        return_value=httpx.Response(200, json={"capabilities": ["completion", "vision"]}))
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    p = ollama.make()
    msgs = [WireMessage("user", [TextBlock(text="what is this"),
                                 ImageBlock(upload_id="u", mime="image/png")], {"u": b"PNGDATA"})]
    for _ in range(2):
        async for _e in p.stream("vis:1b", "", msgs, Options()):
            pass
    m = sent(route)["messages"][0]
    assert m["images"] == [base64.b64encode(b"PNGDATA").decode()]
    assert m["content"] == "what is this" and "image omitted" not in m["content"]
    assert show.call_count == 1  # capability lookup is cached per model


@respx.mock
async def test_capability_lookup_failure_means_no_images():
    respx.post(HOST + "/api/show").mock(side_effect=httpx.ConnectError("x"))
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    msgs = [WireMessage("user", [TextBlock(text="x"), ImageBlock(upload_id="u", mime="image/png")],
                        {"u": b"1"})]
    await run(msgs=msgs)
    m = sent(route)["messages"][1]
    assert "images" not in m and "image omitted" in m["content"]


@respx.mock
async def test_models_survive_a_failing_capability_lookup():
    respx.get(HOST + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "a:1"}]}))
    respx.post(HOST + "/api/show").mock(return_value=httpx.Response(500))
    ms = await ollama.make().models()
    assert [(m.id, m.supports_images) for m in ms] == [("a:1", False)]


@respx.mock
async def test_context_window_defaults_to_8192_when_not_set():
    # Never rely on the Ollama server default: a huge server-side context overloads the host.
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(Options())
    assert sent(route)["options"]["num_ctx"] == 8192


@respx.mock
async def test_explicit_context_window_overrides_the_default():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(Options(context_window=32768))
    assert sent(route)["options"]["num_ctx"] == 32768


@respx.mock
async def test_each_model_offers_the_thinking_controls_ollama_reports_for_it():
    respx.get(HOST + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [
        {"name": "gpt-oss:20b"}, {"name": "qwen3:8b"}, {"name": "llama3.2:3b"}, {"name": "deep:1"}, {"name": "older:1"}]}))
    answers = {b"gpt-oss": show(LEVELS), b"qwen3": show(ON_OFF), b"llama3.2": show({"values": [False], "default": False}),
               b"deep": show({"values": [False, "low", "medium", "high", "max", "ultra"], "default": "high"}), b"older": show()}
    respx.post(HOST + "/api/show").mock(side_effect=lambda req: next(v for k, v in answers.items() if k in req.content))
    by = {m.id: m for m in await ollama.make().models()}
    base = ["temperature", "top_p", "max_tokens", "context_window"]
    assert (by["gpt-oss:20b"].options, by["gpt-oss:20b"].efforts) == (base + ["effort"], ["low", "medium", "high"])
    assert (by["qwen3:8b"].options, by["qwen3:8b"].efforts) == (base + ["thinking"], [])
    assert (by["llama3.2:3b"].options, by["llama3.2:3b"].efforts) == (base, [])               # it does not think
    assert (by["deep:1"].options, by["deep:1"].efforts) == (base + ["thinking", "effort"], ["low", "medium", "high", "max"])
    assert (by["older:1"].options, by["older:1"].efforts) == (base + ["thinking"], [])         # nothing reported: as before


@pytest.mark.parametrize("thinking,options,think", [
    (LEVELS, Options(effort="high"), "high"),
    (LEVELS, Options(effort="max"), "high"),                    # the highest the model names
    (LEVELS, Options(effort="xhigh"), "high"),
    (LEVELS, Options(effort="none"), False),
    (LEVELS, Options(effort="high", thinking=False), False),    # off is off; a model that cannot be off uses its lowest
    (LEVELS, Options(thinking=True), True),
    ({"values": [False, "low", "medium", "high", "max"], "default": "high"}, Options(effort="max"), "max"),
    ({"values": [False, "low", "medium", "high", "max"], "default": "high"}, Options(effort="xhigh"), "max"),
    ({"values": ["low", "high"], "default": "high"}, Options(effort="medium"), "high"),
    (ON_OFF, Options(effort="high"), None),                     # no levels: the effort is not sent
    (ON_OFF, Options(effort="high", thinking=True), True),
    (None, Options(effort="high"), None),                       # an Ollama that reports nothing
    ({"values": [False], "default": False}, Options(effort="high", thinking=True), True),
])
@respx.mock
async def test_effort_is_sent_as_a_level_the_model_names(thinking, options, think):
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    respx.post(HOST + "/api/show").mock(return_value=show(thinking))
    await run(options, model="m:1")
    assert sent(route).get("think") == think and (think is not None or "think" not in sent(route))


@respx.mock
async def test_gpt_oss_on_an_older_ollama_still_gets_its_level():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    respx.post(HOST + "/api/show").mock(return_value=show())
    await run(Options(effort="low"), model="gpt-oss:20b")
    assert sent(route)["think"] == "low"
    respx.get(HOST + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]}))
    m = (await ollama.make().models())[0]
    assert "effort" in m.options and m.efforts == ["low", "medium", "high"]


@respx.mock
async def test_effort_is_left_out_when_the_model_cannot_be_asked():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    respx.post(HOST + "/api/show").mock(side_effect=httpx.ConnectError("x"))
    await run(Options(effort="high"), model="m:1")
    assert "think" not in sent(route)
