"""The colibri provider, held against what colibri's own server sends (tests/colibri_wire.py)."""
import base64
import json

import httpx
import pytest
import respx

from hunterai import context, upstream
from hunterai.providers import colibri, registry
from hunterai.providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta, ToolCall, ToolSpec, Usage,
                                     WireMessage)
from hunterai.types import ImageBlock, TextBlock, ToolCallBlock, ToolResultBlock
from tests import colibri_wire as wire
from tests.proxies import HttpProxy, Site, running

AT = "http://127.0.0.1:8000"
MODELS = AT + "/v1/models"
CHAT = AT + "/v1/chat/completions"
GLM, QWEN = "glm-5.2-colibri", "qwen3.6-colibri"
ASK = [WireMessage("user", [TextBlock(text="hi")])]
FETCH = ToolSpec("web_fetch", "Fetch a page", {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture
def at(monkeypatch):
    monkeypatch.setenv(colibri.URL_ENV, AT)


def listed(text=wire.MODELS_GLM, status=200):
    return respx.get(MODELS).mock(return_value=httpx.Response(status, text=text, headers={"content-type": "application/json"}))


def answers(text, status=200, headers=None):
    return respx.post(CHAT).mock(return_value=httpx.Response(status, text=text, headers=headers or {}))


async def run(model=GLM, msgs=None, options=None, tools=None, system="sys", provider=None):
    p = provider or colibri.make()
    return [e async for e in p.stream(model, system, msgs or ASK, options or Options(), tools=tools)]


async def refused(**how) -> ProviderError:
    with pytest.raises(ProviderError) as e:
        await run(**how)
    return e.value


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


# ---- the address

@pytest.mark.parametrize("written, taken", [
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ("127.0.0.1:8000", "http://127.0.0.1:8000"),
    ("  localhost:8000/ ", "http://localhost:8000"),
    ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000"),            # the "OpenAI base URL" colibri prints, as it is
    ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000"),
    ("https://Box.Lan/colibri/v1", "https://box.lan/colibri"),
    ("http://0.0.0.0:8000", "http://127.0.0.1:8000"),                 # what a log line of the server may say
    ("http://[::1]:8000", "http://[::1]:8000"),
])
def test_an_address_is_taken_as_the_owner_writes_it(written, taken):
    assert colibri.typed(written) == taken


@pytest.mark.parametrize("written, why", [
    ("", "empty"), ("ftp://127.0.0.1:8000", "http://"), ("http://", "host"), ("http://127.0.0.1:99999", "port"),
    ("http://127.0.0.1:0", "port"),
    ("http://me:secret@127.0.0.1:8000", "key"), ("http://127.0.0.1:8000/v1?x=1", "?"), ("http://127.0.0.1:8000/#a", "#"),
    ("http://127.0.0.1 :8000", "space"),
])
def test_what_is_no_address_is_refused_with_the_reason(written, why):
    with pytest.raises(ValueError, match=why.replace("?", r"\?")):
        colibri.typed(written)


def test_without_an_address_colibri_is_not_set_up(monkeypatch):
    assert colibri.address() is None and colibri.make().configured() is False
    monkeypatch.setenv(colibri.URL_ENV, "127.0.0.1:8000/v1")
    assert colibri.address() == AT and colibri.make().configured() is True
    monkeypatch.setenv(colibri.URL_ENV, "ftp://nowhere")          # written into the .env file by hand, and no address
    assert colibri.address() is None and colibri.make().configured() is False


def test_colibri_is_a_provider_like_the_others():
    assert registry.get("colibri").name == "colibri" and registry.get("colibri").supports_tools
    names = [p.name for p in registry.all()]
    assert names.index("colibri") == names.index("ollama") + 1          # the local ones stand together


# ---- what the server says of itself

@respx.mock
async def test_the_server_says_which_model_it_serves_its_context_and_what_it_reads(at):
    listed(wire.MODELS_GLM)
    (model,) = await colibri.make().models()
    assert (model.id, model.label, model.context_window, model.supports_images) == (GLM, GLM, 32768, False)
    assert model.options == ["temperature", "top_p", "max_tokens", "thinking"] and model.efforts == []
    listed(wire.MODELS_QWEN)
    (model,) = await colibri.make().models()
    assert (model.id, model.context_window, model.supports_images) == (QWEN, 65536, True)


@respx.mock
async def test_a_server_that_serves_a_model_has_nothing_to_explain(at):
    listed()
    p = colibri.make()
    assert p.note() is None                                # it was not asked yet
    assert [m.id for m in await p.models()] == [GLM] and p.note() is None


@respx.mock
async def test_a_key_is_sent_only_when_one_is_set(at, monkeypatch):
    route = listed()
    await colibri.make().models()
    assert "authorization" not in route.calls.last.request.headers
    monkeypatch.setenv(colibri.KEY_ENV, "secret-key-1")
    await colibri.make().models()
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key-1"


@respx.mock
async def test_a_server_that_does_not_answer_lists_nothing_and_the_list_says_why(at):
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    p = colibri.make()
    assert await p.models() == []
    seen = await p.served()
    assert (seen.state, seen.at) == ("down", AT)
    assert p.note() == f"nothing answers at {AT}"


@respx.mock
async def test_a_server_that_wants_a_key_lists_nothing_and_the_list_says_so(at, monkeypatch):
    listed(wire.NO_KEY, 401)
    p = colibri.make()
    assert await p.models() == [] and (await p.served()).state == "key"
    assert "key" in p.note() and "Settings" in p.note()
    monkeypatch.setenv(colibri.KEY_ENV, "another-key")
    assert "did not accept" in p.note()


@respx.mock
async def test_a_server_that_refuses_the_name_it_was_asked_by_says_so_in_its_own_words(at):
    listed(wire.OTHER_HOST, 403)
    p = colibri.make()
    assert await p.models() == []
    seen = await p.served()
    assert seen.state == "refused" and "--allowed-host" in seen.said


@respx.mock
@pytest.mark.parametrize("capabilities", [["image_generation"], ["systemone", "decision"]])
async def test_a_model_that_does_not_chat_is_not_offered(at, capabilities):
    entry = {**json.loads(wire.MODELS_GLM)["data"][0], "id": "not-a-chat-model", "capabilities": capabilities}
    listed(json.dumps({"object": "list", "data": [entry]}))
    p = colibri.make()
    assert await p.models() == []
    seen = await p.served()
    assert seen.state == "nochat" and "not-a-chat-model" in p.note()


@respx.mock
@pytest.mark.parametrize("text, status", [("<html>hello</html>", 200), ('{"data": []}', 200), ('{"detail": "Not Found"}', 404),
                                          ('["a"]', 200), ('{"data": [{"name": "x"}]}', 200)])
async def test_what_answers_without_a_model_list_is_not_taken_for_colibri(at, text, status):
    listed(text, status)
    p = colibri.make()
    assert await p.models() == [] and (await p.served()).state == "odd"
    assert AT in p.note() and "colibri" in p.note()


@respx.mock
async def test_a_server_from_before_it_named_capabilities_and_context_is_listed_all_the_same(at):
    listed(json.dumps({"object": "list", "data": [{"id": "glm-5.2-colibri", "object": "model", "owned_by": "colibri"}]}))
    (model,) = await colibri.make().models()
    assert (model.id, model.context_window, model.supports_images) == (GLM, None, False)


@respx.mock
async def test_the_model_list_is_asked_again_only_after_a_while_or_when_the_address_changes(at, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(colibri, "_now", lambda: clock[0])
    route, p = listed(), colibri.make()
    for _ in range(3):
        assert (await p.served()).state == "ok"
    assert route.call_count == 1
    clock[0] += colibri.KEEP + 1
    await p.served()
    assert route.call_count == 2
    await p.models()                                       # the list of models for the page is always asked anew
    assert route.call_count == 3
    other = respx.get("http://127.0.0.1:9000/v1/models").mock(return_value=httpx.Response(200, text=wire.MODELS_QWEN))
    monkeypatch.setenv(colibri.URL_ENV, "http://127.0.0.1:9000")
    seen = await p.served()
    assert other.call_count == 1 and seen.at == "http://127.0.0.1:9000" and seen.models[0].id == QWEN


@respx.mock
async def test_the_window_of_a_call_is_the_context_the_server_holds(at):
    listed(wire.MODELS_QWEN)
    p = colibri.make()
    assert await p.window_for(QWEN, Options()) == 65536
    assert await p.window_for(QWEN, Options(context_window=4096)) == 65536      # the server's, whatever a chat carries along
    assert await p.window_for("another-model", Options()) is None
    registry.override("colibri", p)
    assert await context.window("colibri", QWEN, Options()) == 65536


@respx.mock
async def test_a_server_that_does_not_answer_has_no_window_to_name(at):
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    assert await colibri.make().window_for(GLM, Options()) is None


# ---- an answer

@respx.mock
async def test_a_plain_answer_arrives_as_text_with_its_usage(at):
    listed(), answers(wire.HELLO)
    assert await run() == [TextDelta("Hello"), TextDelta(" from"), TextDelta(" colibri."), Usage(20, 3), Stop("stop")]


@respx.mock
async def test_reasoning_arrives_as_thinking(at):
    listed(), answers(wire.THINK)
    assert await run() == [ThinkingDelta("The user wants a short answer. "), ThinkingDelta("I will give one."),
                           TextDelta("Forty-two"), TextDelta("."), Usage(20, 6), Stop("stop")]


@respx.mock
async def test_what_keeps_the_connection_open_while_the_engine_is_silent_is_not_part_of_the_answer(at):
    listed(wire.MODELS_QWEN), answers(wire.KEEPALIVE)
    assert await run(model=QWEN) == [TextDelta("Late, but here."), Usage(19, 4), Stop("stop")]


@respx.mock
async def test_tool_calls_arrive_with_their_ids_and_arguments(at):
    listed(), answers(wire.TWO_CALLS)
    assert await run(tools=[FETCH]) == [
        TextDelta("I will look"), TextDelta(" at both. "),
        ToolCall("call_589f8dd596574ca3bdef3376", "web_fetch", {"url": 'https://example.com/a?b=1&c="x"'}),
        ToolCall("call_1d3e4e6010cf497da348b79e", "shell", {"command": "echo one\necho two"}),
        Usage(203, 30), Stop("tool_calls")]


@respx.mock
async def test_an_answer_the_engine_ended_at_its_limit_says_so(at):
    listed(), answers(wire.AT_LIMIT)
    assert (await run())[-2:] == [Usage(15, 9), Stop("length")]


@respx.mock
async def test_an_answer_that_stops_without_an_end_is_a_failure_and_not_an_answer(at):
    """colibri's engine exited in the middle: the server sends status 200 and closes, and nothing says it failed."""
    listed(), answers(wire.DIED)
    got = []
    with pytest.raises(ProviderError) as e:
        async for ev in colibri.make().stream(GLM, "sys", ASK, Options()):
            got.append(ev)
    assert got == [TextDelta("The first half of an answer")]            # what came is shown; it is not taken for all of it
    assert e.value.kind == "unavailable" and "without finishing" in e.value.message
    assert not any(isinstance(ev, (Usage, Stop)) for ev in got)


@respx.mock
async def test_tool_calls_of_an_answer_that_did_not_end_are_not_handed_on(at):
    whole = wire.TWO_CALLS
    cut = whole[:whole.index('"finish_reason":"tool_calls"')]
    cut = cut[:cut.rindex("data: ")]                     # everything up to the chunk that ends the answer
    assert "web_fetch" in cut and "shell" in cut
    listed(), answers(cut)
    got = []
    with pytest.raises(ProviderError):
        async for ev in colibri.make().stream(GLM, "sys", ASK, Options(), tools=[FETCH]):
            got.append(ev)
    assert not any(isinstance(ev, ToolCall) for ev in got)


# ---- the request

@respx.mock
async def test_the_request_goes_to_the_address_in_the_shape_colibri_takes(at):
    listed()
    route = answers(wire.HELLO)
    await run(options=Options(temperature=0.2, top_p=0.5, max_tokens=300, context_window=4096, verbosity="low"), tools=[FETCH])
    body = sent(route)
    assert body["model"] == GLM and body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert body["tools"] == [{"type": "function", "function": {"name": "web_fetch", "description": "Fetch a page",
                                                               "parameters": FETCH.parameters}}]
    assert (body["temperature"], body["top_p"], body["max_tokens"]) == (0.2, 0.5, 300)
    assert set(body) == {"model", "stream", "stream_options", "messages", "tools", "temperature", "top_p", "max_tokens"}
    assert "authorization" not in route.calls.last.request.headers
    assert route.calls.last.request.headers["content-length"] == str(len(route.calls.last.request.content))


@respx.mock
async def test_a_tool_round_goes_back_in_the_shape_colibri_reads(at):
    listed()
    route = answers(wire.HELLO)
    msgs = ASK + [WireMessage("assistant", [
        TextBlock(text="let me look"), ToolCallBlock(id="call_589f8dd596574ca3bdef3376", name="web_fetch", arguments={"url": "https://example.com/"}),
        ToolResultBlock(tool_call_id="call_589f8dd596574ca3bdef3376", content="HTTP 200")])]
    await run(msgs=msgs, tools=[FETCH])
    back = sent(route)["messages"][2:]
    assert back[0]["role"] == "assistant" and back[0]["content"] == "let me look"
    assert back[0]["tool_calls"] == [{"id": "call_589f8dd596574ca3bdef3376", "type": "function", "function": {
        "name": "web_fetch", "arguments": '{"url": "https://example.com/"}'}}]
    assert back[1] == {"role": "tool", "tool_call_id": "call_589f8dd596574ca3bdef3376", "content": "HTTP 200"}


@respx.mock
async def test_the_key_goes_with_the_request_when_one_is_set(at, monkeypatch):
    monkeypatch.setenv(colibri.KEY_ENV, "secret-key-1")
    listed()
    route = answers(wire.HELLO)
    await run()
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key-1"


@respx.mock
@pytest.mark.parametrize("options, switch", [(Options(), None), (Options(thinking=True), True), (Options(thinking=False), False),
                                             (Options(thinking=False, effort="max"), False), (Options(effort="xhigh"), None)])
async def test_thinking_is_switched_the_way_colibri_takes_it_and_no_effort_is_sent(at, options, switch):
    """Each family of models takes other effort values and refuses the rest ("max" is refused by all of them)."""
    listed()
    route = answers(wire.HELLO)
    await run(options=options)
    body = sent(route)
    assert "reasoning_effort" not in body
    assert body.get("enable_thinking") is switch and ("enable_thinking" in body) is (switch is not None)


@respx.mock
async def test_a_schema_for_the_answer_is_not_sent(at):
    """colibri does not hold an answer to a schema, and some of its engines refuse a request that has one."""
    listed()
    route = answers(wire.HELLO)
    await run(options=Options(json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}}))
    assert "response_format" not in sent(route)


async def test_colibri_is_asked_directly_whatever_proxy_is_set(monkeypatch):
    """Neither the proxy for the model providers nor one from the environment stands between HunterAI and it."""
    site = Site(lambda host, path: wire.MODELS_GLM if path == "/v1/models" else wire.HELLO, "application/json")
    carrier = HttpProxy(site)
    async with running(site, carrier):
        monkeypatch.setenv(upstream.PROVIDER_ENV, f"http://127.0.0.1:{carrier.port}")
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{carrier.port}")
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(colibri.URL_ENV, f"http://127.0.0.1:{site.port}")
        p = colibri.make()
        assert [m.id for m in await p.models()] == [GLM]
        events = [e async for e in p.stream(GLM, "", ASK, Options())]
    assert TextDelta("Hello") in events and events[-1] == Stop("stop")
    assert [r[:2] for r in site.requests] == [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
    assert carrier.asked == []


@respx.mock
async def test_the_client_waits_longer_than_colibri_lets_a_request_wait_for_its_engine(at, monkeypatch):
    """Nothing is sent, not even the status, while a request waits its turn: five minutes unless colibri was told otherwise."""
    assert colibri.QUEUE_WAIT == 300
    waits = []

    class Watched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            waits.append(kwargs.get("timeout"))
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", Watched)
    listed(), answers(wire.HELLO)
    await run()
    asked, sent_with = waits                               # the list of models, then the request
    assert asked.read <= 10 and sent_with.read >= colibri.QUEUE_WAIT + 30 and sent_with.connect <= 10


# ---- what colibri refuses, in words

@respx.mock
async def test_a_request_that_does_not_fit_names_both_sizes_and_what_to_start_colibri_with(at):
    listed(), answers(wire.TOO_LARGE, 400)
    e = await refused()
    assert e.kind == "bad_request" and e.status == 400
    assert "40,000" in e.message and "32,768" in e.message and "--ctx 65536" in e.message
    assert context.is_overflow(e)                      # so that the request is made smaller and sent once more


@respx.mock
async def test_a_chat_set_to_a_model_colibri_does_not_serve_is_told_which_one_it_serves(at):
    listed(wire.MODELS_QWEN)
    route = answers(wire.HELLO)
    e = await refused(model=GLM)
    assert route.call_count == 0                       # the list already said so: nothing was sent
    assert e.kind == "bad_request" and e.complete and GLM in e.message and QWEN in e.message and "model list" in e.message


@respx.mock
async def test_a_model_that_went_away_between_the_list_and_the_request_is_said_the_same_way(at):
    listed(), answers(wire.NO_SUCH_MODEL.replace("some-other-model", GLM), 404)      # colibri names the model it was asked for
    e = await refused()
    assert e.kind == "bad_request" and e.complete and e.status == 404 and GLM in e.message and "model list" in e.message


@respx.mock
async def test_an_address_that_is_not_colibris_api_is_said_as_that(monkeypatch):
    """The address in Settings leads somewhere, and what is there has no chat endpoint."""
    monkeypatch.setenv(colibri.URL_ENV, "http://127.0.0.1:8000/app")
    respx.get("http://127.0.0.1:8000/app/v1/models").mock(return_value=httpx.Response(404, text='{"detail":"Not Found"}'))
    respx.post("http://127.0.0.1:8000/app/v1/chat/completions").mock(return_value=httpx.Response(
        404, text='{"error":{"message":"Not found.","type":"invalid_request_error","param":null,"code":"not_found"}}'))
    e = await refused()
    assert e.kind == "bad_request" and e.complete and e.status == 404
    assert "http://127.0.0.1:8000/app" in e.message and "Settings" in e.message and "model list" not in e.message


def test_the_context_to_start_colibri_with_is_the_smallest_one_that_holds_what_is_needed():
    assert colibri.context_to_start(17192, 16384) == 32768
    assert colibri.context_to_start(38192, 32768) == 65536
    assert colibri.context_to_start(17192, 32768) is None          # what it has is enough
    assert colibri.context_to_start(17192, None) is None           # it did not say what it has
    assert colibri.context_to_start(308192, 131072) == 262144      # nothing holds it: the largest there is
    assert colibri.context_to_start(308192, 262144) is None        # and there is none above that


@respx.mock
async def test_a_key_colibri_does_not_accept_is_said_before_anything_is_sent(at, monkeypatch):
    listed(wire.NO_KEY, 401)
    route = answers(wire.HELLO)
    e = await refused()
    assert route.call_count == 0 and e.kind == "auth" and e.complete and colibri.KEY_ENV in e.message and "Settings" in e.message
    assert "asks for a key" in e.message
    monkeypatch.setenv(colibri.KEY_ENV, "another-key")
    assert "did not accept the key" in (await refused()).message


@respx.mock
async def test_a_key_refused_for_the_request_itself_is_said_the_same_way(at):
    listed(), answers(wire.NO_KEY, 401)
    e = await refused()
    assert e.kind == "auth" and e.complete and e.status == 401 and colibri.KEY_ENV in e.message


@respx.mock
async def test_a_host_name_colibri_was_not_told_to_answer_to_is_said_in_its_own_words(at):
    listed(wire.OTHER_HOST, 403)
    route = answers(wire.HELLO)
    e = await refused()
    assert route.call_count == 0 and e.complete and "--allowed-host mybox.lan" in e.message and "colibri" in e.message
    assert e.kind == "bad_request"                     # not "policy": no model refused anything


@respx.mock
async def test_a_host_name_refused_for_the_request_itself_is_said_the_same_way(at):
    listed(), answers(wire.OTHER_HOST, 403)
    e = await refused()
    assert e.kind == "bad_request" and e.complete and e.status == 403 and "--allowed-host mybox.lan" in e.message and AT in e.message


@respx.mock
async def test_a_model_that_does_not_chat_is_not_sent_a_chat(at):
    entry = {**json.loads(wire.MODELS_GLM)["data"][0], "id": "an-image-model", "capabilities": ["image_generation"]}
    listed(json.dumps({"object": "list", "data": [entry]}))
    route = answers(wire.HELLO)
    e = await refused(model="an-image-model")
    assert route.call_count == 0 and e.kind == "bad_request" and e.complete
    assert "an-image-model" in e.message and "not a chat model" in e.message and "--reconfigure" in e.message


def too_large(limit, used) -> str:
    return wire.TOO_LARGE.replace("32768", str(limit)).replace("40000", str(used))


@respx.mock
@pytest.mark.parametrize("limit, used, told", [
    (32768, 40000, "--ctx 65536"),            # the next context holds it with room for an answer
    (32768, 60000, "--ctx 131072"),           # the next one would hold the request and little of an answer: one further
    (131072, 250000, "--ctx 262144"),         # only the largest holds it at all
    (262144, 300000, "offers no context that holds it"),
    (131072, 300000, "offers no context that holds it"),          # there is a larger one, and it would not hold it either
])
async def test_the_context_named_for_a_request_that_does_not_fit_leaves_room_for_the_answer(at, limit, used, told):
    listed(), answers(too_large(limit, used), 400)
    e = await refused()
    assert told in e.message and f"{used:,}" in e.message and f"{limit:,}" in e.message and context.is_overflow(e)
    assert ("--ctx" in e.message) is told.startswith("--ctx")


@respx.mock
async def test_a_refusal_for_size_in_words_colibri_does_not_use_today_is_still_one_for_size(at):
    listed(), answers('{"error":{"message":"The prompt is longer than the context.","type":"invalid_request_error","param":"messages",'
                      '"code":"context_length_exceeded"}}', 400)
    e = await refused()
    assert e.kind == "bad_request" and "The prompt is longer than the context" in e.message and context.is_overflow(e)


@respx.mock
@pytest.mark.parametrize("body, said", [(wire.QUEUE_TIMEOUT, "busy with other requests"), (wire.QUEUE_FULL, "queue is full")])
async def test_a_busy_colibri_says_that_it_is_busy_and_how_long_to_wait(at, body, said):
    listed(), answers(body, 429, {"retry-after": "1"})
    e = await refused()
    assert e.kind == "rate_limit" and e.retry_after == 1.0 and e.complete and said in e.message and "key" not in e.message
    # a chat on colibri has no subagents (tests/test_local_alone.py): the way out is not a setting about them
    assert "send the message again when it is free" in e.message and "subagent" not in e.message


@respx.mock
async def test_a_failure_on_colibris_side_keeps_its_status_and_words(at):
    listed(), answers('{"error":{"message":"The inference scheduler is shutting down.","type":"server_error","param":null,'
                      '"code":"scheduler_closed"}}', 503, {"retry-after": "5"})
    e = await refused()
    assert e.kind == "unavailable" and e.status == 503 and "shutting down" in e.message and not e.complete


@respx.mock
async def test_what_colibri_refuses_for_the_model_it_serves_keeps_its_words(at):
    listed(), answers(wire.TEXT_ONLY, 400)
    e = await refused()
    assert e.kind == "bad_request" and e.status == 400 and "text message content only" in e.message and not e.complete


@respx.mock
async def test_a_colibri_that_does_not_answer_is_unavailable_and_says_where_it_was_asked(at):
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    e = await refused()
    assert e.kind == "unavailable" and e.status is None and AT in e.message and not e.complete


@respx.mock
async def test_a_list_that_fails_once_does_not_stop_a_request_colibri_would_answer(at, monkeypatch):
    """The list is asked for what the model reads and holds. Whether colibri answers is for the request to find out."""
    clock = [1000.0]
    monkeypatch.setattr(colibri, "_now", lambda: clock[0])
    listed(wire.MODELS_QWEN), answers(wire.HELLO)
    p = colibri.make()
    await run(model=QWEN, provider=p)
    clock[0] += colibri.KEEP + 1
    respx.get(MODELS).mock(side_effect=httpx.ReadTimeout("slow"))
    route = answers(wire.HELLO)
    picture = [WireMessage("user", [TextBlock(text="what is this"), ImageBlock(upload_id="u1", mime="image/png")], images={"u1": PNG})]
    assert TextDelta("Hello") in await run(model=QWEN, msgs=picture, provider=p)
    assert sent(route)["messages"][-1]["content"][1]["type"] == "image_url"       # what the list said before still holds


@respx.mock
async def test_without_an_address_a_chat_set_to_colibri_says_where_to_set_one(monkeypatch):
    e = await refused()
    assert e.kind == "bad_request" and e.complete and "Settings" in e.message and "address" in e.message


@respx.mock
async def test_no_answer_in_time_is_a_timeout(at):
    listed()
    respx.post(CHAT).mock(side_effect=httpx.ReadTimeout("nothing came"))
    e = await refused()
    assert e.kind == "timeout" and "colibri" in e.message


@respx.mock
async def test_a_request_that_is_stopped_is_closed_at_once(at, monkeypatch):
    """The owner presses Stop: the connection to colibri is closed there and then, and colibri is free for the next
    request. Left to the garbage collector, its one engine would go on writing an answer nobody reads."""
    from hunterai.providers import openai_compat
    closed = []

    async def endless(self, model, system, messages, options, tools=None):
        try:
            while True:
                yield TextDelta("more")
        finally:
            closed.append(True)
    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "stream", endless)
    listed()
    events = colibri.make().stream(GLM, "sys", ASK, Options())
    assert await events.__anext__() == TextDelta("more")
    await events.aclose()
    assert closed == [True]


# ---- pictures

def shot(upload, text="look"):
    return WireMessage("user", [TextBlock(text=text), ImageBlock(upload_id=upload, mime="image/png")], images={upload: PNG})


@respx.mock
async def test_only_the_latest_picture_is_sent_and_the_earlier_ones_are_named(at):
    """colibri holds one picture for a request, and refuses a request with two."""
    listed(wire.MODELS_QWEN)
    route = answers(wire.HELLO)
    msgs = [shot("u1", "first"), WireMessage("assistant", [TextBlock(text="a login page")]),
            WireMessage("user", [TextBlock(text="and these two"), ImageBlock(upload_id="u2", mime="image/png"),
                                 ImageBlock(upload_id="u3", mime="image/png")], images={"u2": PNG, "u3": PNG})]
    await run(model=QWEN, msgs=msgs)
    m = sent(route)["messages"]
    pictures = [p for message in m if isinstance(message["content"], list) for p in message["content"] if p["type"] == "image_url"]
    assert len(pictures) == 1 and pictures[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert m[3]["content"][-1] == pictures[0]                              # the last one of the last message
    for message, upload in ((m[1], "u1"), (m[3], "u2")):
        told = message["content"] if isinstance(message["content"], str) else " ".join(p.get("text", "") for p in message["content"])
        assert "one image" in told and f'image="upload:{upload}"' in told   # and how the model can still have it read
    assert "first" in json.dumps(m[1]) and "and these two" in json.dumps(m[3])


@respx.mock
async def test_a_picture_whose_file_is_gone_is_not_the_one_that_is_sent(at):
    listed(wire.MODELS_QWEN)
    route = answers(wire.HELLO)
    msgs = [shot("u1"), WireMessage("user", [TextBlock(text="and this"), ImageBlock(upload_id="gone", mime="image/png")])]
    await run(model=QWEN, msgs=msgs)
    m = sent(route)["messages"]
    assert [p["type"] for p in m[1]["content"]] == ["text", "image_url"]
    assert "image_url" not in json.dumps(m[2])


@respx.mock
async def test_a_model_that_reads_text_only_gets_no_picture(at):
    listed(wire.MODELS_GLM)
    route = answers(wire.HELLO)
    await run(msgs=[shot("u1")])
    content = sent(route)["messages"][1]["content"]
    assert isinstance(content, str) and "look_at_image" in content and "image_url" not in content


@respx.mock
async def test_a_request_larger_than_colibri_reads_is_not_sent_and_says_what_fills_it(at):
    listed(wire.MODELS_QWEN)
    route = answers(wire.HELLO)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (3_300_000)
    msgs = [WireMessage("user", [TextBlock(text="what is on it"), ImageBlock(upload_id="u1", mime="image/png")], images={"u1": big})]
    e = await refused(model=QWEN, msgs=msgs)
    assert route.call_count == 0 and e.kind == "bad_request" and e.complete
    assert "4.2 MB" in e.message and "4 MB" in e.message and "picture" in e.message and "look_at_image" in e.message


@respx.mock
async def test_a_request_just_under_what_colibri_reads_is_sent(at):
    listed(wire.MODELS_QWEN)
    route = answers(wire.HELLO)
    fits = b"\x89PNG\r\n\x1a\n" + b"\x00" * (3_000_000)
    msgs = [WireMessage("user", [TextBlock(text="what is on it"), ImageBlock(upload_id="u1", mime="image/png")], images={"u1": fits})]
    await run(model=QWEN, msgs=msgs)
    assert route.call_count == 1 and len(route.calls.last.request.content) < colibri.MAX_BODY
