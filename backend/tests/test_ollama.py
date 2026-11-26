import json

import httpx
import pytest
import respx

from hunterai import context
from hunterai.providers import ollama
from hunterai.providers.base import (SAMPLING, Options, ProviderError, Stop, TextDelta, ThinkingDelta, ToolSpec,
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


def a_plain_model():
    """Before a call the provider asks Ollama what the model is: a plain one, unless the test said what it answers."""
    if not any("/api/show" in repr(r.pattern) for r in respx.routes):
        respx.post(HOST + "/api/show").mock(return_value=show())


async def run(options=None, msgs=None, model="qwen3:8b", system="sys", tools=None):
    a_plain_model()
    msgs = msgs or [WireMessage("user", [TextBlock(text="hi")])]
    return [e async for e in ollama.make().stream(model, system, msgs, options or Options(), tools=tools)]


def sent(route):
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_ndjson_parsed_to_events():
    respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    assert await run() == [ThinkingDelta("hm"), TextDelta("Hi"), Usage(9, 2), Stop("stop")]


def show(thinking=None, capabilities=("completion",), context_length=None):
    """What /api/show answers: `thinking` is the object newer Ollama versions report, None for an older one;
    `context_length` is the most tokens the model takes, as its file says."""
    info = {} if context_length is None else {"model_info": {"general.architecture": "qwen3", "qwen3.context_length": context_length,
                                                            "qwen3.embedding_length": 4096}}
    return httpx.Response(200, json={"capabilities": list(capabilities), **info, **({} if thinking is None else {"thinking": thinking})})


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
    assert "think" not in sent(route) and sent(route)["options"] == {"num_ctx": ollama.DEFAULT_NUM_CTX}


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
    assert ms[0].options == ["temperature", "top_p", "max_tokens", "context_window", *SAMPLING, "thinking"]
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
    a_plain_model()
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
    asked = respx.post("http://127.0.0.1:11500/api/show").mock(return_value=show())
    await run()
    assert route.called and asked.called


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
async def test_a_call_gets_the_window_for_local_models_when_the_chat_sets_none():
    # Never left to the Ollama server: its own default follows the machine's memory, from 4,096 to 256,000 tokens.
    # 8,192, the default before, is filled by HunterAI's rules and tool list alone.
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(Options())
    assert sent(route)["options"]["num_ctx"] == 16384 == ollama.DEFAULT_NUM_CTX


@respx.mock
async def test_explicit_context_window_overrides_the_default():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(Options(context_window=32768))
    assert sent(route)["options"]["num_ctx"] == 32768


@respx.mock
async def test_the_owner_sets_the_window_for_every_local_model(monkeypatch):
    monkeypatch.setenv("HUNTERAI_OLLAMA_NUM_CTX", "32768")
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(Options())
    assert sent(route)["options"]["num_ctx"] == 32768 == ollama.default_num_ctx()
    await run(Options(context_window=4096))             # a chat's own setting still comes first
    assert sent(route)["options"]["num_ctx"] == 4096


@pytest.mark.parametrize("written", ["", "many", "-5", "0", "1000", "16k", "3e4"])
def test_a_window_setting_that_names_no_usable_size_is_not_used(monkeypatch, written):
    monkeypatch.setenv("HUNTERAI_OLLAMA_NUM_CTX", written)
    assert ollama.default_num_ctx() == ollama.DEFAULT_NUM_CTX


@respx.mock
async def test_a_model_is_never_given_a_larger_window_than_it_takes():
    """Ollama would lower it by itself and say nothing, and HunterAI would fit the chat to the wrong size."""
    respx.post(HOST + "/api/show").mock(return_value=show(context_length=8192))
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run(Options())
    assert sent(route)["options"]["num_ctx"] == 8192
    await run(Options(context_window=65536))
    assert sent(route)["options"]["num_ctx"] == 8192
    await run(Options(context_window=4096))             # a smaller one is the owner's choice
    assert sent(route)["options"]["num_ctx"] == 4096


@respx.mock
async def test_the_window_of_a_call_is_what_the_call_will_send(monkeypatch):
    respx.post(HOST + "/api/show").mock(side_effect=lambda req: show(context_length=8192 if b"small" in req.content else 131072))
    p = ollama.make()
    assert await p.window_for("small:1b", Options()) == 8192
    assert await p.window_for("large:8b", Options()) == 16384
    assert await p.window_for("large:8b", Options(context_window=40000)) == 40000
    monkeypatch.setenv("HUNTERAI_OLLAMA_NUM_CTX", "24576")
    assert await p.window_for("large:8b", Options()) == 24576


@respx.mock
async def test_the_window_is_the_wanted_one_while_ollama_cannot_say_what_the_model_takes():
    asked = respx.post(HOST + "/api/show").mock(side_effect=httpx.ConnectError("x"))
    p = ollama.make()
    assert await p.window_for("qwen3:8b", Options()) == 16384
    assert await p.window_for("qwen3:8b", Options()) == 16384 and asked.call_count == 2      # asked again: not known yet


@respx.mock
async def test_each_model_is_listed_with_the_window_a_call_to_it_gets():
    respx.get(HOST + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": "small:1b"}, {"name": "large:8b"}]}))
    respx.post(HOST + "/api/show").mock(side_effect=lambda req: show(context_length=8192 if b"small" in req.content else 131072))
    assert [(m.id, m.context_window) for m in await ollama.make().models()] == [("small:1b", 8192), ("large:8b", 16384)]


@respx.mock
async def test_ollama_is_told_not_to_drop_or_cut_anything_itself():
    """By default it removes the oldest messages, then tokens from the middle, and reports neither."""
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    await run()
    assert sent(route)["truncate"] is False and sent(route)["shift"] is False


@respx.mock
async def test_a_request_larger_than_the_window_is_not_sent():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    history = [WireMessage("user" if i % 2 == 0 else "assistant", [TextBlock(text="word " * 500)]) for i in range(21)]
    with pytest.raises(ProviderError) as ei:              # 52,500 characters for a window of 8,192 tokens
        await run(Options(context_window=8192), msgs=history)
    assert not route.called
    assert context.is_overflow(ei.value)                  # the engine makes the request smaller and asks once more
    assert "8,192" in ei.value.message and "Context window for local models" in ei.value.message


@respx.mock
async def test_the_tool_definitions_count_towards_what_has_to_fit():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    tools = [ToolSpec(f"tool_{i}", "does one thing " * 20, {"type": "object", "properties": {"x": {"type": "string", "description": "y" * 300}}})
             for i in range(40)]
    with pytest.raises(ProviderError) as ei:
        await run(Options(context_window=8192), tools=tools)
    assert not route.called and context.is_overflow(ei.value)
    await run(Options(context_window=16384), tools=tools)
    assert route.called and len(sent(route)["tools"]) == 40


@respx.mock
async def test_a_request_that_fits_the_window_is_sent_as_it_is():
    route = respx.post(URL).mock(return_value=httpx.Response(200, text=BODY))
    history = [WireMessage("user" if i % 2 == 0 else "assistant", [TextBlock(text="word " * 500)]) for i in range(9)]
    await run(Options(context_window=8192), msgs=history)      # 22,500 characters
    assert [m["role"] for m in sent(route)["messages"]] == ["system"] + ["user", "assistant"] * 4 + ["user"]


@pytest.mark.parametrize("refusal", [
    "request (9000 tokens) exceeds the available context size (8192 tokens), try increasing it",
    "the prompt is longer than the context length currently available to the model; shorten the prompt, adjust the context "
    "length in settings, or use a model with a longer context length",
])
@respx.mock
async def test_a_refusal_by_ollama_for_size_is_one_the_engine_knows(refusal):
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": refusal}))
    with pytest.raises(ProviderError) as ei:
        await run()
    assert context.is_overflow(ei.value) and refusal[:40] in ei.value.message


@respx.mock
async def test_a_picture_the_program_behind_the_model_cannot_read_is_said_in_words_the_owner_can_act_on():
    """What Ollama answers then, as it was reported. The owner was told to change the model or an option, which
    does nothing for it: the picture goes with every turn of the chat."""
    respx.post(URL).mock(return_value=httpx.Response(400, json={"error": {
        "code": 400, "message": "Failed to load image or audio file", "type": "invalid_request_error"}}))
    with pytest.raises(ProviderError) as ei:
        await run(model="eyes:9b")
    e = ei.value
    assert e.kind == "bad_request" and e.status == 400 and e.complete
    assert "Failed to load image or audio file" in e.message and "eyes:9b" in e.message
    assert "new chat without the picture" in e.message and "another model" in e.message
    assert not context.is_overflow(e)


@respx.mock
async def test_each_model_offers_the_thinking_controls_ollama_reports_for_it():
    respx.get(HOST + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [
        {"name": "gpt-oss:20b"}, {"name": "qwen3:8b"}, {"name": "llama3.2:3b"}, {"name": "deep:1"}, {"name": "older:1"}]}))
    answers = {b"gpt-oss": show(LEVELS), b"qwen3": show(ON_OFF), b"llama3.2": show({"values": [False], "default": False}),
               b"deep": show({"values": [False, "low", "medium", "high", "max", "ultra"], "default": "high"}), b"older": show()}
    respx.post(HOST + "/api/show").mock(side_effect=lambda req: next(v for k, v in answers.items() if k in req.content))
    by = {m.id: m for m in await ollama.make().models()}
    base = ["temperature", "top_p", "max_tokens", "context_window", *SAMPLING]
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


# ---- a call Ollama does not answer in time

@respx.mock
async def test_a_call_ollama_sent_nothing_for_says_how_long_that_was():
    """Seen in a bench on the owner's laptop on 27 November 2026 as "the model call failed: Ollama timed out", which
    reads as if the bench had broken. Ollama sends nothing while it loads a model, reads a long request, or while the
    model writes a tool call; five minutes of that is where this adapter stops waiting."""
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("no bytes"))
    with pytest.raises(ProviderError) as e:
        await run()
    assert (e.value.kind, e.value.message) == ("timeout", "Ollama timed out: it sent nothing for 5 minutes")
    assert ollama.TIMEOUT.read == 300


@respx.mock
async def test_a_connection_that_ollama_did_not_take_in_time_says_only_that_it_timed_out():
    respx.post(URL).mock(side_effect=httpx.ConnectTimeout("no connection"))
    with pytest.raises(ProviderError) as e:
        await run()
    assert (e.value.kind, e.value.message) == ("timeout", "Ollama timed out")
