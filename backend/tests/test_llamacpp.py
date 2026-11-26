"""The llama.cpp provider, held against what a real llama-server sent (tests/llamacpp_wire.py)."""
import base64
import json

import httpx
import pytest
import respx

from hunterai import context, upstream
from hunterai.providers import llamacpp, registry
from hunterai.providers.base import (
    SAMPLING,
    Options,
    ProviderError,
    Stop,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolSpec,
    Usage,
    WireMessage,
)
from hunterai.types import ImageBlock, TextBlock, ToolCallBlock, ToolResultBlock
from tests import llamacpp_wire as wire
from tests.proxies import HttpProxy, Site, running

AT = "http://127.0.0.1:9931"
MODELS = AT + "/v1/models"
PROPS = AT + "/props"
CHAT = AT + "/v1/chat/completions"
QWEN, STORY = "qwen3.5-0.8b", "tiny"
ASK = [WireMessage("user", [TextBlock(text="hi")])]
SHELL = ToolSpec("shell", "Run a command", {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]})
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture
def at(monkeypatch):
    monkeypatch.setenv(llamacpp.URL_ENV, AT)


def listed(text=wire.MODELS_QWEN, status=200):
    return respx.get(MODELS).mock(return_value=httpx.Response(status, text=text, headers={"content-type": "application/json"}))


def answers(text, status=200, headers=None):
    return respx.post(CHAT).mock(return_value=httpx.Response(status, text=text, headers=headers or {}))


async def run(model=QWEN, msgs=None, options=None, tools=None, system="sys", provider=None):
    p = provider or llamacpp.make()
    return [e async for e in p.stream(model, system, msgs or ASK, options or Options(), tools=tools)]


async def refused(**how) -> ProviderError:
    with pytest.raises(ProviderError) as e:
        await run(**how)
    return e.value


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


def entries(text) -> list[dict]:
    return json.loads(text)["data"]


def a_list(*some) -> str:
    return json.dumps({"object": "list", "data": list(some)})


# ---- the address

@pytest.mark.parametrize("written, taken", [
    ("http://127.0.0.1:9931", "http://127.0.0.1:9931"),
    ("127.0.0.1:8080", "http://127.0.0.1:8080"),                      # the port of a llama-server from before October 2026
    ("  localhost:9931/ ", "http://localhost:9931"),
    ("http://127.0.0.1:9931/v1", "http://127.0.0.1:9931"),            # the base URL the OpenAI clients are given
    ("http://127.0.0.1:9931/v1/", "http://127.0.0.1:9931"),
    ("https://Box.Lan/llama/v1", "https://box.lan/llama"),
    ("http://0.0.0.0:9931", "http://127.0.0.1:9931"),                 # what a log line of the server may say
    ("http://[::1]:9931", "http://[::1]:9931"),
])
def test_an_address_is_taken_as_the_owner_writes_it(written, taken):
    assert llamacpp.typed(written) == taken


@pytest.mark.parametrize("written, why", [
    ("", "empty"), ("ftp://127.0.0.1:9931", "http://"), ("http://", "host"), ("http://127.0.0.1:99999", "port"),
    ("http://127.0.0.1:0", "port"),
    ("http://me:secret@127.0.0.1:9931", "key"), ("http://127.0.0.1:9931/v1?x=1", "?"), ("http://127.0.0.1:9931/#a", "#"),
    ("http://key@127.0.0.1:9931", "key"), ("http://:secret@127.0.0.1:9931", "key"),
    ("http://127.0.0.1 :9931", "space"), ("http://127.0.0.1:9931/a\x01", "control"), ("http://127.0.0.1\x7f:9931", "control"),
])
def test_what_is_no_address_is_refused_with_the_reason(written, why):
    with pytest.raises(ValueError, match=why.replace("?", r"\?")):
        llamacpp.typed(written)


def test_without_an_address_llama_cpp_is_not_set_up(monkeypatch):
    assert llamacpp.address() is None and llamacpp.make().configured() is False
    monkeypatch.setenv(llamacpp.URL_ENV, "127.0.0.1:9931/v1")
    assert llamacpp.address() == AT and llamacpp.make().configured() is True
    monkeypatch.setenv(llamacpp.URL_ENV, "ftp://nowhere")          # written into the .env file by hand, and no address
    assert llamacpp.address() is None and llamacpp.make().configured() is False


def test_llama_cpp_is_a_provider_like_the_others():
    assert registry.get("llamacpp").name == "llamacpp" and registry.get("llamacpp").supports_tools
    names = [p.name for p in registry.all()]
    assert names.index("llamacpp") == names.index("ollama") + 2 == names.index("colibri") + 1          # the local ones stand together


# ---- what a server with one model says of itself

@respx.mock
async def test_the_server_says_which_model_it_serves_its_context_and_what_it_reads(at):
    listed(wire.MODELS_QWEN)
    (model,) = await llamacpp.make().models()
    assert (model.id, model.label, model.context_window, model.supports_images) == (QWEN, QWEN, 8192, True)
    assert model.options == ["temperature", "top_p", "max_tokens", *SAMPLING, "thinking"] and model.efforts == []
    listed(wire.MODELS_STORY)
    (model,) = await llamacpp.make().models()
    assert (model.id, model.context_window, model.supports_images) == (STORY, 256, False)


def test_the_context_in_the_list_is_the_one_a_single_request_has():
    """Started with -c 512 -np 2, a request has 256 tokens and not 512: the list and /props both say 256."""
    assert wire.SLOTS == [("-c 512", 4, 512, 512), ("-c 512 -np 2", 2, 256, 256), ("-c 2048 -np 4", 4, 512, 512)]


@respx.mock
async def test_a_server_that_serves_a_model_has_nothing_to_explain(at):
    listed()
    p = llamacpp.make()
    assert p.note() is None                                # it was not asked yet
    assert [m.id for m in await p.models()] == [QWEN] and p.note() is None
    seen = await p.served()
    assert (seen.state, seen.at, seen.router) == ("ok", AT, False) and [h.loaded for h in seen.held] == [True]


@respx.mock
async def test_a_key_is_sent_only_when_one_is_set(at, monkeypatch):
    route = listed()
    await llamacpp.make().models()
    assert "authorization" not in route.calls.last.request.headers
    monkeypatch.setenv(llamacpp.KEY_ENV, "secret-key-1")
    await llamacpp.make().models()
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key-1"


@respx.mock
async def test_a_server_that_does_not_answer_lists_nothing_and_the_list_says_why(at):
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    p = llamacpp.make()
    assert await p.models() == []
    seen = await p.served()
    assert (seen.state, seen.at) == ("down", AT)
    assert p.note() == f"nothing answers at {AT}"


@respx.mock
async def test_a_server_that_is_still_loading_its_model_says_so(at):
    listed(wire.LOADING, 503)
    p = llamacpp.make()
    assert await p.models() == [] and (await p.served()).state == "loading"
    assert p.note() == "it is still loading its model"


@respx.mock
async def test_a_server_that_wants_a_key_lists_nothing_and_the_list_says_so(at, monkeypatch):
    listed(wire.NO_KEY, 401)
    p = llamacpp.make()
    assert await p.models() == [] and (await p.served()).state == "key"
    assert "key" in p.note() and "Settings" in p.note() and "--api-key" in p.note()
    monkeypatch.setenv(llamacpp.KEY_ENV, "another-key")
    assert "did not accept" in p.note()


@respx.mock
@pytest.mark.parametrize("writes", [["audio"], ["decisions"]])
async def test_a_model_that_writes_no_text_is_not_offered(at, writes):
    entry = {**entries(wire.MODELS_QWEN)[0], "id": "not-a-chat-model", "architecture": {"input_modalities": ["text"], "output_modalities": writes}}
    listed(a_list(entry))
    p = llamacpp.make()
    assert await p.models() == []
    seen = await p.served()
    assert seen.state == "nomodel" and "not-a-chat-model" in p.note() and "no text" in p.note()


@respx.mock
@pytest.mark.parametrize("text, status", [("<html>hello</html>", 200), ('{"detail": "Not Found"}', 404), ('["a"]', 200),
                                          ('{"data": [{"name": "x"}]}', 200), ('{"data": "none"}', 200), (wire.NOT_THERE, 404)])
async def test_what_answers_without_a_model_list_is_not_taken_for_llama_server(at, text, status):
    listed(text, status)
    p = llamacpp.make()
    assert await p.models() == [] and (await p.served()).state == "odd"
    assert AT in p.note() and "llama-server" in p.note()
    assert (await p.served()).said == ("it lists no model" if status == 200 else f"it answers HTTP {status}")


@respx.mock
@pytest.mark.parametrize("text, status", [
    ('{"error":{"code":503,"message":"Service Unavailable","type":"unavailable_error"}}', 503),      # busy is not loading
    ('{"error":{"code":500,"message":"error loading model: out of memory","type":"server_error"}}', 500),   # nor is a load that failed
])
async def test_an_error_that_is_not_the_one_of_a_model_being_loaded_is_not_said_to_be_one(at, text, status):
    listed(text, status)
    seen = await llamacpp.make().served()
    assert (seen.state, seen.said) == ("odd", f"it answers HTTP {status}")


@respx.mock
async def test_a_server_from_before_the_list_named_the_context_is_asked_for_its_properties(at):
    """The list of an older llama-server has no n_ctx and no architecture. /props had both long before."""
    old = {k: v for k, v in entries(wire.MODELS_QWEN)[0].items() if k != "architecture"}
    old["meta"] = {k: v for k, v in old["meta"].items() if k != "n_ctx"}
    listed(a_list(old))
    props = respx.get(PROPS).mock(return_value=httpx.Response(200, text=wire.PROPS_QWEN))
    (model,) = await llamacpp.make().models()
    assert (model.id, model.context_window, model.supports_images) == (QWEN, 8192, True)
    assert props.call_count == 1
    listed(a_list(old))
    props.mock(return_value=httpx.Response(200, text=wire.PROPS_STORY))
    (model,) = await llamacpp.make().models()
    assert (model.context_window, model.supports_images) == (256, False)


@respx.mock
async def test_what_the_list_of_an_older_server_does_say_is_kept_and_only_the_rest_is_asked(at):
    whole = entries(wire.MODELS_QWEN)[0]
    no_context = {**whole, "aliases": ["small"], "meta": {k: v for k, v in whole["meta"].items() if k != "n_ctx"}}
    listed(a_list(no_context))                              # it says what the model reads, and not its context
    respx.get(PROPS).mock(return_value=httpx.Response(200, text=wire.PROPS_STORY))      # 256 tokens, no pictures
    p = llamacpp.make()
    (model,) = await p.models()
    assert (model.context_window, model.supports_images) == (256, True)
    assert (await p.served()).named("small").info.id == QWEN          # its other names are kept
    no_reading = {k: v for k, v in whole.items() if k != "architecture"}
    listed(a_list(no_reading))                              # it says the context, and not what the model reads
    respx.get(PROPS).mock(return_value=httpx.Response(200, text=json.dumps(
        {"default_generation_settings": {"n_ctx": 256}, "modalities": {"vision": True}})))
    (model,) = await llamacpp.make().models()
    assert (model.context_window, model.supports_images) == (8192, True)


@respx.mock
async def test_a_list_that_says_it_all_asks_for_nothing_more(at):
    listed()
    props = respx.get(PROPS).mock(return_value=httpx.Response(200, text=wire.PROPS_QWEN))
    await llamacpp.make().models()
    assert props.call_count == 0


@respx.mock
@pytest.mark.parametrize("answer", [httpx.Response(401, text=wire.NO_KEY), httpx.Response(200, text="<html>"), httpx.Response(200, text='{"modalities": 3}'),
                                    httpx.Response(200, text='{"default_generation_settings": {"n_ctx": 0}, "modalities": {"vision": "yes"}}')])
async def test_an_older_server_whose_properties_cannot_be_had_is_listed_without_them(at, answer):
    listed(a_list({"id": "old-model", "object": "model", "owned_by": "llamacpp"}))
    respx.get(PROPS).mock(return_value=answer)
    (model,) = await llamacpp.make().models()
    assert (model.id, model.context_window, model.supports_images) == ("old-model", None, False)


@respx.mock
async def test_an_older_server_that_breaks_off_while_it_is_asked_for_its_properties_is_listed_without_them(at):
    listed(a_list({"id": "old-model", "object": "model", "owned_by": "llamacpp"}))
    respx.get(PROPS).mock(side_effect=httpx.ReadError("reset"))
    (model,) = await llamacpp.make().models()
    assert (model.id, model.context_window) == ("old-model", None)


@respx.mock
async def test_the_model_list_is_asked_again_only_after_a_while_or_when_the_address_changes(at, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(llamacpp, "_now", lambda: clock[0])
    route, p = listed(), llamacpp.make()
    for _ in range(3):
        assert (await p.served()).state == "ok"
    assert route.call_count == 1
    clock[0] += llamacpp.KEEP - 1
    await p.served()
    assert route.call_count == 1
    clock[0] += 2
    await p.served()
    assert route.call_count == 2
    await p.models()                                       # the list of models for the page is always asked anew
    assert route.call_count == 3
    other = respx.get("http://127.0.0.1:9000/v1/models").mock(return_value=httpx.Response(200, text=wire.MODELS_STORY))
    monkeypatch.setenv(llamacpp.URL_ENV, "http://127.0.0.1:9000")
    seen = await p.served()
    assert other.call_count == 1 and seen.at == "http://127.0.0.1:9000" and seen.models[0].id == STORY
    monkeypatch.delenv(llamacpp.URL_ENV)
    assert (await p.served()).state == "off" and p.note() is None


@respx.mock
async def test_what_was_said_of_one_address_is_not_said_of_another(at, monkeypatch):
    """The address is read from the .env file each time: the owner may change it there while HunterAI runs."""
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    p = llamacpp.make()
    await p.served()
    assert p.note() == f"nothing answers at {AT}"
    monkeypatch.setenv(llamacpp.URL_ENV, "http://127.0.0.1:9000")
    assert p.note() is None


@respx.mock
async def test_the_window_of_a_call_is_the_context_the_server_holds(at):
    listed()
    p = llamacpp.make()
    assert await p.window_for(QWEN, Options()) == 8192
    assert await p.window_for(QWEN, Options(context_window=4096)) == 8192      # the server's, whatever a chat carries along
    assert await p.window_for("another-model", Options()) is None
    registry.override("llamacpp", p)
    assert await context.window("llamacpp", QWEN, Options()) == 8192


@respx.mock
async def test_a_server_that_does_not_answer_has_no_window_to_name(at):
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    assert await llamacpp.make().window_for(QWEN, Options()) is None


@respx.mock
async def test_a_server_that_serves_nothing_is_not_asked_for_its_properties(at):
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    props = respx.get(PROPS).mock(return_value=httpx.Response(200, text=wire.PROPS_ASLEEP))
    p = llamacpp.make()
    assert await p.properties() == {} and await p.sleeping() is False and await p.has_tools() is None
    assert props.call_count == 0


@respx.mock
async def test_a_sleeping_server_lists_its_model_as_before(at):
    """Started with --sleep-idle-seconds, it frees the memory after a while and loads the model again for the next chat."""
    listed(wire.MODELS_ASLEEP)
    (model,) = await llamacpp.make().models()
    assert (model.id, model.context_window) == (STORY, 256)


# ---- a router: one server, the models of a folder

@respx.mock
async def test_a_router_lists_every_model_of_its_folder_and_says_which_is_loaded(at):
    listed(wire.ROUTER_NONE)
    p = llamacpp.make()
    assert [m.id for m in await p.models()] == ["story-a", "story-b"]
    seen = await p.served()
    assert seen.router is True and [(h.info.id, h.loaded) for h in seen.held] == [("story-a", False), ("story-b", False)]
    listed(wire.ROUTER_A)
    seen = await p.served(fresh=True)
    assert [(h.info.id, h.loaded) for h in seen.held] == [("story-a", True), ("story-b", False)]
    listed(wire.ROUTER_LOADING)
    assert [(h.info.id, h.loaded) for h in (await p.served(fresh=True)).held] == [("story-a", False), ("story-b", False)]


@respx.mock
async def test_a_model_the_router_has_not_loaded_has_the_context_its_instance_will_be_started_with(at):
    """The router was started with -c 256: the command of each instance says --ctx-size 256. The 2048 in the list is
    the length the model was trained with, which is not what a request gets."""
    listed(wire.ROUTER_NONE)
    assert [m.context_window for m in await llamacpp.make().models()] == [256, 256]
    assert all(e["context_length"] == 2048 for e in entries(wire.ROUTER_NONE))
    listed(wire.ROUTER_A)                                   # loaded: what it holds now
    assert [m.context_window for m in await llamacpp.make().models()] == [256, 256]


def router_entry(args, **more) -> dict:
    """A model of a router's folder, not loaded, with the command its instance will be started with. Trained with a
    context larger than any a test starts it with, unless the test says otherwise."""
    entry = json.loads(json.dumps(entries(wire.ROUTER_NONE)[0]))
    entry["context_length"] = 262144
    entry["status"]["args"] = ["/opt/llama.cpp/bin/llama-server", "--host", "127.0.0.1", "--port", "0", "--alias", "story-a", *args,
                               "--model", "/models/story-a.gguf"]
    entry["status"].update(more)
    return entry


@respx.mock
@pytest.mark.parametrize("args, window", [
    ([], None),                                              # no context given: the instance takes what fits when it loads
    (["--ctx-size", "8192"], 8192),
    (["--ctx-size", "0"], None),                             # 0: from the model, as much as fits
    (["--ctx-size", "8192", "--parallel", "4"], 2048),       # four slots share it (see SLOTS)
    (["--ctx-size", "512", "--parallel", "2"], 256),
    (["--ctx-size", "-1"], None),
    (["--ctx-size", "8192", "--parallel", "1"], 8192),
    (["--ctx-size", "8192", "--parallel", "-1"], 8192),      # -1: slots that share the whole context
    (["--ctx-size", "many"], None), (["--ctx-size"], None), (["--parallel", "4"], None),
])
async def test_the_context_of_a_model_that_is_not_loaded_is_read_from_the_command_of_its_instance(at, args, window):
    listed(a_list(router_entry(args)))
    (model,) = await llamacpp.make().models()
    assert model.context_window == window


@respx.mock
@pytest.mark.parametrize("args, trained, window", [
    (["--ctx-size", "8192"], 2048, 2048),                      # seen: -c 4096 on a model trained with 2,048 gives 2,048
    (["--ctx-size", "8192", "--parallel", "2"], 2048, 2048),   # seen: -c 8192 -np 2 gives 2,048 each, not 4,096
    (["--ctx-size", "1024"], 2048, 1024),
    (["--ctx-size", "8192", "--parallel", "4"], 4096, 2048),
    (["--ctx-size", "8192"], None, 8192), (["--ctx-size", "8192"], 0, 8192), (["--ctx-size", "8192"], "2048", 8192),
])
async def test_a_model_gets_no_more_context_than_it_was_trained_with(at, args, trained, window):
    """llama-server holds a request to the length the model was trained with, however large -c is. The list says that
    length for a model that is not loaded (context_length)."""
    entry = router_entry(args)
    entry["context_length"] = trained
    listed(a_list(entry))
    (model,) = await llamacpp.make().models()
    assert model.context_window == window


@respx.mock
async def test_what_a_loaded_model_says_it_holds_goes_before_the_command_it_was_started_with(at):
    """llama-server gives a model no more context than it was trained with: started with -c 4096, the story model
    holds 2,048."""
    loaded = router_entry(["--ctx-size", "4096"], value="loaded")
    loaded["meta"] = {"n_ctx": 2048}
    listed(a_list(loaded))
    (model,) = await llamacpp.make().models()
    assert model.context_window == 2048


@respx.mock
@pytest.mark.parametrize("said", [True, 0, -5, "8192", 8192.0, None])
async def test_a_context_that_is_no_count_of_tokens_is_not_taken_for_one(at, said):
    entry = router_entry([], value="loaded")
    entry["meta"] = {"n_ctx": said}
    listed(a_list(entry))
    (model,) = await llamacpp.make().models()
    assert model.context_window is None


@respx.mock
async def test_a_model_the_router_is_still_downloading_is_not_offered_and_one_that_failed_to_load_is(at):
    coming = router_entry([], value="downloading", progress={"https://example.com/m.gguf": {"done": 1, "total": 2}})
    coming["id"] = "coming"
    half = router_entry([], value="incomplete")
    half["id"] = "half"
    broke = router_entry([], failed=True, exit_code=1)
    broke["id"] = "broke"
    listed(a_list(coming, half, broke, entries(wire.ROUTER_NONE)[1]))
    p = llamacpp.make()
    assert [m.id for m in await p.models()] == ["broke", "story-b"]
    assert [(h.info.id, h.failed) for h in (await p.served()).held] == [("broke", True), ("story-b", False)]


@respx.mock
async def test_a_router_that_is_still_downloading_all_it_has_says_which(at):
    coming = [{**router_entry([], value="downloading"), "id": name} for name in ("one", "two", "three", "four")]
    coming[1]["status"]["value"] = "incomplete"
    listed(a_list(*coming))
    p = llamacpp.make()
    assert await p.models() == [] and (await p.served()).state == "nomodel"
    assert p.note() == "it is still downloading one, two, three"          # three names are enough


@respx.mock
async def test_a_router_whose_only_finished_model_writes_no_text_names_that_one(at):
    coming = {**router_entry([], value="downloading"), "id": "coming"}
    talker = {**router_entry([]), "id": "talker", "architecture": {"input_modalities": ["text"], "output_modalities": ["audio"]}}
    listed(a_list(coming, talker))
    p = llamacpp.make()
    assert await p.models() == []
    assert p.note() == "it serves talker, which writes no text (a model for speech or for decisions): start it with a chat model"


@respx.mock
async def test_a_router_with_an_empty_folder_says_so(at):
    listed('{"data":[],"object":"list"}')
    p = llamacpp.make()
    assert await p.models() == [] and (await p.served()).state == "nomodel"
    assert p.note() == "it lists no model: a llama-server started without one serves the GGUF files of the folder it was given (--models-dir)"


@respx.mock
async def test_a_router_is_not_asked_for_properties_it_does_not_have(at):
    """/props of a router is the router's own (wire.ROUTER_PROPS): no context, no model."""
    assert json.loads(wire.ROUTER_PROPS)["role"] == "router"
    bare = {k: v for k, v in entries(wire.ROUTER_NONE)[0].items() if k != "architecture"}
    bare["status"] = {"value": "unloaded"}
    listed(a_list(bare))
    props = respx.get(PROPS).mock(return_value=httpx.Response(200, text=wire.ROUTER_PROPS))
    (model,) = await llamacpp.make().models()
    assert (model.context_window, model.supports_images) == (None, False) and props.call_count == 0


@respx.mock
async def test_a_model_is_known_by_its_other_names_too(at):
    entry = {**entries(wire.MODELS_QWEN)[0], "id": "/models/Qwen3.5-0.8B-Q4_K_M.gguf", "aliases": ["qwen", "small"]}
    listed(a_list(entry))
    p = llamacpp.make()
    assert [m.id for m in await p.models()] == ["/models/Qwen3.5-0.8B-Q4_K_M.gguf"]
    assert await p.window_for("small", Options()) == 8192
    route = answers(wire.PLAIN)
    assert (await run(model="qwen", provider=p))[-1] == Stop("stop") and sent(route)["model"] == "qwen"


# ---- an answer

@respx.mock
async def test_a_plain_answer_arrives_as_text_with_its_usage(at):
    listed(), answers(wire.PLAIN)
    assert await run() == [TextDelta("5"), Usage(39, 2), Stop("stop")]


@respx.mock
async def test_reasoning_arrives_as_thinking_and_then_the_answer(at):
    listed(), answers(wire.THINK)
    got = await run()
    thoughts = [e.text for e in got if isinstance(e, ThinkingDelta)]
    assert thoughts == ["Thinking", " Process", ":", "\n", "5", '"']          # the recording keeps its first four chunks and its last two
    assert got[len(thoughts):] == [TextDelta("5"), Usage(37, 227), Stop("stop")]
    assert all(isinstance(e, ThinkingDelta) for e in got[:len(thoughts)])


@respx.mock
async def test_a_tool_call_arrives_whole_from_its_pieces(at):
    listed(), answers(wire.ONE_CALL)
    assert await run(tools=[SHELL]) == [ToolCall("8WwuxRyBSfuj4SinLzJx9LvuZDricNrK", "shell", {"command": "id"}), Usage(360, 26),
                                        Stop("tool_calls")]


@respx.mock
async def test_thinking_comes_before_the_call_it_led_to(at):
    listed(), answers(wire.THINK_THEN_CALL)
    got = await run(tools=[SHELL])
    assert isinstance(got[0], ThinkingDelta) and got[0].text == "The"
    assert got[-3:] == [ToolCall("1ZLlL6pNYudrduaFsIolSIGhCNuMfmh3", "shell", {"command": "id"}), Usage(358, 56), Stop("tool_calls")]
    assert not any(isinstance(e, TextDelta) for e in got)


@respx.mock
async def test_two_calls_in_one_answer_arrive_as_two_with_their_own_ids_in_their_order(at):
    listed(), answers(wire.TWO_CALLS)
    assert await run(tools=[SHELL]) == [
        ToolCall("tF6XrBkTuWd4WTbEXoho2TCgTeZQNqHI", "shell", {"command": "id"}),
        ToolCall("5CMink7ujlJC5TXvGR7jhjR9tqf2iTis", "web_fetch", {"url": "https://example.com/a"}),
        Usage(394, 56), Stop("tool_calls")]


@respx.mock
async def test_an_answer_ended_at_its_limit_says_so(at):
    listed(), answers(wire.AT_LIMIT)
    got = await run()
    assert "".join(e.text for e in got if isinstance(e, TextDelta)) == "1. two\n2" and got[-2:] == [Usage(34, 5), Stop("length")]


@respx.mock
async def test_an_answer_that_stops_without_an_end_is_a_failure_and_not_an_answer(at):
    """A stream that closes under a status of 200 with no chunk that ends the answer."""
    listed(wire.MODELS_STORY), answers(wire.KILLED)
    got = []
    with pytest.raises(ProviderError) as e:
        async for ev in llamacpp.make().stream(STORY, "sys", ASK, Options()):
            got.append(ev)
    assert got and all(isinstance(ev, TextDelta) for ev in got)         # what came is shown; it is not taken for all of it
    assert e.value.kind == "unavailable" and e.value.complete and "without finishing" in e.value.message
    assert "The terminal it runs in says why" in e.value.message


@respx.mock
async def test_a_server_killed_in_the_middle_of_an_answer_is_said_the_same_way(at):
    """What a client sees when llama-server is killed while it writes: the body breaks off (an incomplete read)."""
    listed(wire.MODELS_STORY)
    respx.post(CHAT).mock(side_effect=httpx.RemoteProtocolError("peer closed connection without sending complete message body"))
    e = await refused(model=STORY)
    assert e.kind == "unavailable" and e.complete and "without finishing" in e.message and AT in e.message


@respx.mock
async def test_tool_calls_of_an_answer_that_did_not_end_are_not_handed_on(at):
    whole = wire.TWO_CALLS
    cut = whole[:whole.index('"finish_reason":"tool_calls"')]
    cut = cut[:cut.rindex("data: ")]                     # everything up to the chunk that ends the answer
    assert "web_fetch" in cut and "shell" in cut
    listed(), answers(cut)
    got = []
    with pytest.raises(ProviderError):
        async for ev in llamacpp.make().stream(QWEN, "sys", ASK, Options(), tools=[SHELL]):
            got.append(ev)
    assert not any(isinstance(ev, ToolCall) for ev in got)


# ---- the request

@respx.mock
async def test_the_request_goes_to_the_address_in_the_shape_llama_server_takes(at):
    listed()
    route = answers(wire.PLAIN)
    await run(options=Options(temperature=0.2, top_p=0.5, max_tokens=300, context_window=4096, verbosity="low", effort="high"), tools=[SHELL])
    body = sent(route)
    assert body["model"] == QWEN and body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert body["tools"] == [{"type": "function", "function": {"name": "shell", "description": "Run a command", "parameters": SHELL.parameters}}]
    assert (body["temperature"], body["top_p"], body["max_tokens"]) == (0.2, 0.5, 300)
    assert set(body) == {"model", "stream", "stream_options", "messages", "tools", "temperature", "top_p", "max_tokens"}
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_a_tool_round_goes_back_in_the_shape_llama_server_reads(at):
    listed()
    route = answers(wire.PLAIN)
    msgs = ASK + [WireMessage("assistant", [
        TextBlock(text="let me look"), ToolCallBlock(id="8WwuxRyBSfuj4SinLzJx9LvuZDricNrK", name="shell", arguments={"command": "id"}),
        ToolResultBlock(tool_call_id="8WwuxRyBSfuj4SinLzJx9LvuZDricNrK", content="uid=1000")])]
    await run(msgs=msgs, tools=[SHELL])
    back = sent(route)["messages"][2:]
    assert back[0]["role"] == "assistant" and back[0]["content"] == "let me look"
    assert back[0]["tool_calls"] == [{"id": "8WwuxRyBSfuj4SinLzJx9LvuZDricNrK", "type": "function", "function": {
        "name": "shell", "arguments": '{"command": "id"}'}}]
    assert back[1] == {"role": "tool", "tool_call_id": "8WwuxRyBSfuj4SinLzJx9LvuZDricNrK", "content": "uid=1000"}


@respx.mock
async def test_the_key_goes_with_the_request_when_one_is_set(at, monkeypatch):
    monkeypatch.setenv(llamacpp.KEY_ENV, "secret-key-1")
    listed()
    route = answers(wire.PLAIN)
    await run()
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key-1"


@respx.mock
@pytest.mark.parametrize("options, effort, kwargs", [
    (Options(), None, None),                                              # as the server has it: Qwen3.5 thinks
    (Options(thinking=True), None, {"enable_thinking": True}),
    (Options(thinking=False), "none", None),
    (Options(thinking=False, effort="max"), "none", None),
    (Options(effort="xhigh"), None, None),                                # a template takes its own values and refuses others
])
async def test_thinking_is_switched_the_way_llama_server_takes_it_and_no_other_effort_is_sent(at, options, effort, kwargs):
    listed()
    route = answers(wire.PLAIN)
    await run(options=options)
    body = sent(route)
    assert body.get("reasoning_effort") == effort and ("reasoning_effort" in body) is (effort is not None)
    assert body.get("chat_template_kwargs") == kwargs and ("chat_template_kwargs" in body) is (kwargs is not None)


@respx.mock
async def test_a_schema_for_the_answer_is_sent_in_the_shape_the_recorded_request_had(at):
    listed()
    route = answers(wire.SCHEMA)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    got = await run(options=Options(json_schema=schema))
    assert sent(route)["response_format"] == {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
    assert json.loads("".join(e.text for e in got if isinstance(e, TextDelta))) == {"ok": True}


@respx.mock
async def test_pictures_go_to_a_model_that_reads_them_and_as_a_line_to_one_that_does_not(at):
    look = [WireMessage("user", [TextBlock(text="what is this"), ImageBlock(upload_id="u1", mime="image/png"),
                                 ImageBlock(upload_id="u2", mime="image/png")], images={"u1": PNG, "u2": PNG})]
    listed(wire.MODELS_QWEN)
    route = answers(wire.PICTURE)
    got = await run(msgs=look)
    assert got == [TextDelta("red"), Usage(32, 2), Stop("stop")]
    parts = sent(route)["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "image_url"]          # llama-server takes several in one request
    assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    listed(wire.MODELS_STORY)
    route = answers(wire.PLAIN)
    await run(model=STORY, msgs=look)
    assert isinstance(sent(route)["messages"][1]["content"], str) and "image" in sent(route)["messages"][1]["content"]


def picture() -> list[WireMessage]:
    return [WireMessage("user", [TextBlock(text="what is this"), ImageBlock(upload_id="u1", mime="image/png")], images={"u1": PNG})]


@respx.mock
async def test_a_list_that_fails_once_does_not_take_away_what_the_model_reads(at, monkeypatch):
    """The list is asked for again every half minute. When that one time nothing comes, the request is sent all the same
    (it finds out itself), written for the model as the server last described it."""
    clock = [1000.0]
    monkeypatch.setattr(llamacpp, "_now", lambda: clock[0])
    p = llamacpp.make()
    listed(wire.MODELS_QWEN)
    route = answers(wire.PICTURE)
    await run(msgs=picture(), provider=p)
    respx.get(MODELS).mock(side_effect=httpx.ReadTimeout("slow"))
    clock[0] += llamacpp.KEEP + 1
    await run(msgs=picture(), provider=p)
    assert route.call_count == 2 and (await p.served()).state == "down"
    assert [part["type"] for part in sent(route)["messages"][1]["content"]] == ["text", "image_url"]


@respx.mock
async def test_a_model_no_list_named_is_sent_no_picture(at, monkeypatch):
    """What a model reads is known from the server's list alone: one of a server that never listed it, and one of
    another address than the list came from, is taken to read text."""
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    route = answers(wire.PLAIN)
    await run(msgs=picture())
    assert isinstance(sent(route)["messages"][1]["content"], str) and "look_at_image" in sent(route)["messages"][1]["content"]
    p = llamacpp.make()
    listed(wire.MODELS_QWEN)
    await run(msgs=picture(), provider=p)
    assert [part["type"] for part in sent(route)["messages"][1]["content"]] == ["text", "image_url"]
    monkeypatch.setenv(llamacpp.URL_ENV, "http://127.0.0.1:9000")              # another server now, which lists nothing
    respx.get("http://127.0.0.1:9000/v1/models").mock(side_effect=httpx.ConnectError("refused"))
    there = respx.post("http://127.0.0.1:9000/v1/chat/completions").mock(return_value=httpx.Response(200, text=wire.PLAIN))
    await run(msgs=picture(), provider=p)
    assert isinstance(sent(there)["messages"][1]["content"], str)


async def test_llama_server_is_asked_directly_whatever_proxy_is_set(monkeypatch):
    """Neither the proxy for the model providers nor one from the environment stands between HunterAI and it."""
    site = Site(lambda host, path: wire.MODELS_QWEN if path == "/v1/models" else wire.PLAIN, "application/json")
    carrier = HttpProxy(site)
    async with running(site, carrier):
        monkeypatch.setenv(upstream.PROVIDER_ENV, f"http://127.0.0.1:{carrier.port}")
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{carrier.port}")
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(llamacpp.URL_ENV, f"http://127.0.0.1:{site.port}")
        p = llamacpp.make()
        assert [m.id for m in await p.models()] == [QWEN]
        events = [e async for e in p.stream(QWEN, "", ASK, Options())]
    assert TextDelta("5") in events and events[-1] == Stop("stop")
    assert [r[:2] for r in site.requests] == [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
    assert carrier.asked == []


@respx.mock
async def test_the_client_waits_for_a_model_that_has_to_be_loaded_first_and_not_long_for_the_list(at, monkeypatch):
    """A router loads the model of a chat before it answers, and sends nothing until then."""
    waits = []

    class Watched(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            waits.append(kwargs.get("timeout"))
            super().__init__(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", Watched)
    listed(), answers(wire.PLAIN)
    await run()
    asked, sent_with = waits                               # the list of models, then the request
    assert asked.read <= 10 and sent_with.read >= 600 and sent_with.connect <= 10


# ---- what llama-server refuses, in words

@respx.mock
async def test_a_request_that_does_not_fit_names_both_sizes_and_how_to_start_llama_server_with_more(at):
    listed(wire.MODELS_STORY), answers(wire.TOO_LONG, 400)
    e = await refused(model=STORY)
    assert e.kind == "bad_request" and e.status == 400 and e.complete is False
    assert "1,841" in e.message and "256" in e.message and "-c 4096" in e.message and "more memory" in e.message
    assert context.is_overflow(e)                      # so that the request is made smaller and sent once more


@respx.mock
@pytest.mark.parametrize("more", [{}, {"n_ctx": 256}, {"n_prompt_tokens": 1841}, {"n_ctx": "many", "n_prompt_tokens": 1841}])
async def test_a_refusal_for_size_that_does_not_name_both_sizes_keeps_the_servers_words(at, more):
    error = {"code": 400, "message": "request exceeds the available context size", "type": "exceed_context_size_error", **more}
    listed(), answers(json.dumps({"error": error}), 400)
    e = await refused()
    assert e.message == ("The request does not fit the context window of the model llama-server holds (request exceeds the "
                         "available context size)")
    assert e.kind == "bad_request" and e.status == 400 and context.is_overflow(e)


@respx.mock
@pytest.mark.parametrize("tokens, window, start", [(1841, 256, 4096), (5000, 4096, 8192), (9000, 8192, 12288), (40000, 32768, 53248),
                                                   (6554, 8192, 12288), (6553, 8192, 8192), (100, 20000, 20480), (100, 16384, 16384)])
async def test_the_context_to_start_with_leaves_room_for_the_answer(at, tokens, window, start):
    """The next step that holds the request and a quarter more, and never less than what it has."""
    assert llamacpp.context_to_start(tokens, window) == start


@pytest.mark.parametrize("tokens, size", [(1, 4096), (4096, 4096), (4097, 8192), (33800, 36864), (65536, 65536), (65537, 69632)])
def test_a_context_is_named_in_steps_of_4096_tokens_and_not_doubled(tokens, size):
    """llama-server takes any size and reserves the memory of all of it when it starts: the next power of two above
    33,800 tokens would be 65,536, some gigabytes more on a laptop card than the 36,864 that hold them."""
    assert llamacpp.context_for(tokens) == size


@respx.mock
async def test_after_a_refusal_for_size_the_server_is_asked_anew_what_it_holds(at):
    """A router model that was not loaded had no context to name. The refusal came from its instance: it is loaded now."""
    route = listed(wire.ROUTER_NONE)
    answers(wire.TOO_LONG, 400)
    p = llamacpp.make()
    await p.served()
    with pytest.raises(ProviderError):
        await run(model="story-a", provider=p)
    assert route.call_count == 1
    listed(wire.ROUTER_A)
    assert await p.window_for("story-a", Options()) == 256
    assert respx.get(MODELS).call_count == 2


@respx.mock
async def test_a_chat_set_to_a_model_the_server_does_not_serve_is_told_which_one_it_serves(at):
    """A llama-server with one model answers any model name with the model it holds: the chat would show another name."""
    listed(wire.MODELS_QWEN)
    route = answers(wire.PLAIN)
    e = await refused(model="gemma-4-12b")
    assert route.call_count == 0                       # the list already said so: nothing was sent
    assert e.kind == "bad_request" and e.complete and "gemma-4-12b" in e.message and QWEN in e.message and "model list" in e.message


@respx.mock
async def test_a_chat_set_to_a_model_the_router_does_not_have_is_told_which_ones_it_has(at):
    listed(wire.ROUTER_NONE)
    route = answers(wire.PLAIN)
    e = await refused(model="story-c")
    assert route.call_count == 0 and e.complete and "story-c" in e.message and "story-a, story-b" in e.message


@respx.mock
async def test_a_model_that_left_the_folder_between_the_list_and_the_request_is_said_the_same_way(at):
    listed(wire.ROUTER_NONE), answers(wire.ROUTER_NO_SUCH.replace("no-such-model", "story-a"), 400)
    e = await refused(model="story-a")
    assert e.kind == "bad_request" and e.complete and e.status == 400 and "story-a" in e.message and "model list" in e.message


@respx.mock
async def test_a_model_that_is_not_loaded_on_a_router_that_loads_none_by_itself_says_how_to_change_that(at):
    listed(wire.ROUTER_NONE), answers(wire.ROUTER_NOT_LOADED, 400)
    e = await refused(model="story-a")
    assert e.kind == "bad_request" and e.complete and "--no-models-autoload" in e.message and "story-a" in e.message


@respx.mock
async def test_a_server_that_is_still_loading_its_model_asks_to_send_again(at):
    listed(wire.LOADING, 503), answers(wire.LOADING, 503)
    e = await refused()
    assert e.kind == "unavailable" and e.status == 503 and e.complete and "loading its model" in e.message and "again" in e.message


@respx.mock
async def test_a_server_that_wants_a_key_says_so_before_and_after_the_request(at, monkeypatch):
    listed(wire.NO_KEY, 401)
    route = answers(wire.NO_KEY, 401)
    e = await refused()
    assert route.call_count == 0 and e.kind == "auth" and "--api-key" in e.message and "Settings" in e.message
    listed(wire.MODELS_QWEN)                                # the list answered, and the request did not: the key was changed between
    monkeypatch.setenv(llamacpp.KEY_ENV, "old-key")
    e = await refused(provider=llamacpp.make())
    assert route.call_count == 1 and e.kind == "auth" and e.status == 401 and "did not accept" in e.message
    assert "old-key" not in e.message


@respx.mock
async def test_a_picture_for_a_model_without_its_projector_names_the_projector(at):
    listed(), answers(wire.NO_EYES, 500)
    e = await refused()
    assert e.kind == "bad_request" and e.complete and "--mmproj" in e.message and "projector" in e.message


@respx.mock
async def test_a_picture_the_server_could_not_decode_is_said_as_that(at):
    listed(), answers(wire.BAD_PICTURE, 400)
    e = await refused()
    assert e.kind == "bad_request" and e.complete and "could not read" in e.message and "PNG or JPEG" in e.message


@respx.mock
async def test_an_address_that_is_not_llama_servers_api_is_said_as_that(monkeypatch):
    monkeypatch.setenv(llamacpp.URL_ENV, "http://127.0.0.1:9931/app")
    respx.get("http://127.0.0.1:9931/app/v1/models").mock(return_value=httpx.Response(404, text=wire.NOT_THERE))
    respx.post("http://127.0.0.1:9931/app/v1/chat/completions").mock(return_value=httpx.Response(404, text=wire.NOT_THERE))
    e = await refused()
    assert e.kind == "bad_request" and e.complete and e.status == 404
    assert "http://127.0.0.1:9931/app" in e.message and "Settings" in e.message and "model list" not in e.message


@respx.mock
async def test_without_an_address_nothing_is_asked_and_the_error_says_where_to_set_it():
    route = answers(wire.PLAIN)
    e = await refused()
    assert route.call_count == 0 and e.kind == "bad_request" and e.complete and "Settings" in e.message and "llama.cpp" in e.message


@respx.mock
async def test_a_server_that_went_away_is_said_with_its_address(at):
    listed()
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    e = await refused()
    assert e.kind == "unavailable" and not e.complete and e.message == f"llama.cpp does not answer at {AT} (ConnectError)"


@respx.mock
async def test_a_server_that_sends_nothing_in_time_is_named_as_the_owner_knows_it(at):
    listed()
    respx.post(CHAT).mock(side_effect=httpx.ReadTimeout("timed out"))
    e = await refused()
    assert e.kind == "timeout" and e.message == f"llama-server at {AT} did not answer in time" and not e.complete


@respx.mock
async def test_a_refusal_it_has_no_words_for_keeps_the_servers_own(at):
    listed(), answers('{"error":{"code":400,"message":"\'messages\' is required","type":"invalid_request_error"}}', 400)
    e = await refused()
    assert e.kind == "bad_request" and e.status == 400 and not e.complete and "'messages' is required" in e.message


@respx.mock
@pytest.mark.parametrize("body, words", [('{"error": "the words of something else"}', "the words of something else"),
                                         ('["not", "an", "object"]', "not"), ("3", "3"), ("plain words", "plain words"),
                                         ('{"error": {"message": 7}}', "7")])
async def test_a_refusal_written_another_way_than_llama_server_writes_one_keeps_its_words(at, body, words):
    listed(), answers(body, 400)
    e = await refused()
    assert e.kind == "bad_request" and e.status == 400 and words in e.message


@respx.mock
@pytest.mark.parametrize("text, status", [
    ('{"error":{"code":503,"message":"Service Unavailable","type":"unavailable_error"}}', 503),
    ('{"error":{"code":500,"message":"error loading model: out of memory","type":"server_error"}}', 500),
])
async def test_a_failure_that_is_not_a_model_being_loaded_is_not_told_to_wait_for_one(at, text, status):
    listed(), answers(text, status)
    e = await refused()
    assert e.status == status and "still loading" not in e.message and json.loads(text)["error"]["message"] in e.message


@respx.mock
async def test_a_chat_on_a_server_that_has_nothing_to_chat_with_is_told_so_and_nothing_is_sent(at):
    listed('{"data":[],"object":"list"}')
    route = answers(wire.PLAIN)
    e = await refused(model="story-a")
    assert "This chat is set to the llama.cpp model story-a, which llama-server does not have now." in e.message
    assert route.call_count == 0 and e.complete


@respx.mock
async def test_a_server_that_is_down_or_odd_is_still_sent_the_request_which_finds_out_itself(at):
    """A list that failed once says little: the server may have come up since."""
    respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))
    route = answers(wire.PLAIN)
    assert (await run())[-1] == Stop("stop") and route.call_count == 1
