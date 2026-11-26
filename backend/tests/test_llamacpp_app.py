"""llama.cpp in the app: its address and key in Settings, what the page and the checks say about it, the words for
its errors, the rules for a local model, and a chat that is answered by it. The server is a stand-in that sends what
a real llama-server sent (tests/llamacpp_wire.py)."""
import json

import httpx
import pytest
import respx

from hunterai import config, context, cooling, db, engine, problems, smoke, subagents
from hunterai.api import system
from hunterai.providers import llamacpp, registry
from hunterai.providers.base import ProviderError
from hunterai.repo import chats, messages, runs, settings
from hunterai.types import TextBlock
from tests import llamacpp_wire as wire
from tests.test_api import client  # noqa: F401 (client is a fixture)
from tests.test_llamacpp import a_list, entries, router_entry
from tests.test_smoke import run as smoke_run
from tests.test_smoke import said as smoke_said

AT = "http://127.0.0.1:9931"
MODELS = AT + "/v1/models"
PROPS = AT + "/props"
CHAT = AT + "/v1/chat/completions"
SERVER = "http://127.0.0.1:8790"
QWEN, STORY = "qwen3.5-0.8b", "tiny"


@pytest.fixture
def at(monkeypatch):
    monkeypatch.setenv(llamacpp.URL_ENV, AT)


def listed(text=wire.MODELS_QWEN, status=200):
    return respx.get(MODELS).mock(return_value=httpx.Response(status, text=text, headers={"content-type": "application/json"}))


def props(text=wire.PROPS_QWEN, status=200):
    return respx.get(PROPS).mock(return_value=httpx.Response(status, text=text, headers={"content-type": "application/json"}))


def down():
    return respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))


def with_context(tokens: int, text=wire.MODELS_QWEN) -> str:
    entry = json.loads(text)["data"][0]
    entry["meta"]["n_ctx"] = tokens
    return json.dumps({"object": "list", "data": [entry]})


def chunks(text: str, ended: str = "stop") -> str:
    """An answer in the chunks llama-server writes."""
    def one(delta, finish=None):
        return "data: " + json.dumps({"choices": [{"finish_reason": finish, "index": 0, "delta": delta}], "created": 1,
                                      "id": "chatcmpl-1", "model": QWEN, "object": "chat.completion.chunk"}) + "\n\n"
    usage = "data: " + json.dumps({"choices": [], "usage": {"completion_tokens": 9, "prompt_tokens": 50, "total_tokens": 59}}) + "\n\n"
    return one({"role": "assistant", "content": None}) + one({"content": text}) + one({}, ended) + usage + "data: [DONE]\n\n"


def failed(kind, message, status=None, complete=False) -> ProviderError:
    e = ProviderError(kind, message, status, complete=complete)
    e.provider, e.model = "llamacpp", QWEN
    return e


def fixed_overhead(monkeypatch, tokens: int):
    async def overhead(conn):
        return {"tokens": tokens, "tools": 20, "on_demand": 0, "window": 16384}
    monkeypatch.setattr(system, "overhead", overhead)


# ---- its errors, in words

def test_a_llama_server_that_does_not_answer_is_told_to_start_it():
    said = problems.model_call(failed("unavailable", f"llama.cpp does not answer at {AT} (ConnectError)"))
    assert said.startswith(f"llama.cpp does not answer at {AT} (ConnectError). ")
    assert "Start llama-server" in said and "Settings" in said and "internet" not in said


def test_a_failure_llama_server_answered_with_is_not_told_to_start_what_runs():
    said = problems.model_call(failed("unavailable", "HTTP 500: Compute error.", 500))
    assert said.startswith("llama.cpp: HTTP 500: Compute error. ")
    assert "Start llama-server" not in said and "the terminal it runs in says why" in said and "send the message again" in said


def test_a_llama_server_that_sent_nothing_in_time_is_said_to_be_loading_or_busy():
    said = problems.model_call(failed("timeout", f"llama-server at {AT} did not answer in time"))
    assert said.startswith(f"llama-server at {AT} did not answer in time. ")
    assert "has to be loaded first" in said and "provider is slow or down" not in said
    named = problems.model_call(failed("unavailable", f"llama-server at {AT} broke off"))        # either of its names is its name
    assert named.startswith(f"llama-server at {AT} broke off. ")


def test_what_llama_server_did_not_accept_points_at_the_model_and_its_template():
    said = problems.model_call(failed("bad_request", "HTTP 400: 'messages' is required", 400))
    assert said.startswith("llama.cpp: HTTP 400: 'messages' is required. ")
    assert "chat template" in said and "pick another one" not in said


def test_an_error_that_says_what_to_do_itself_is_left_as_it_is():
    whole = "llama-server at http://127.0.0.1:9931 is still loading its model. Send the message again in a moment."
    assert problems.model_call(failed("unavailable", whole, 503, complete=True)) == whole


def test_the_other_providers_keep_their_words():
    e = ProviderError("unavailable", "openai unreachable: ConnectError")
    e.provider = "openai"
    assert "check the internet connection" in problems.model_call(e)
    e = ProviderError("unavailable", "colibri does not answer at http://127.0.0.1:8000 (ConnectError)")
    e.provider = "colibri"
    assert "./start-here.sh" in problems.model_call(e)


def test_an_answer_llama_server_cut_off_names_its_window_and_how_to_start_it_with_a_larger_one():
    kind, text = problems.cut_off("length", provider="llamacpp", window=8192)
    assert kind == "cut_off" and "8,192 tokens" in text and "-c 12288;" in text and "llama-server" in text
    assert "the request and the answer" in text and "Context window for local models" not in text
    assert "-c 8192;" in problems.cut_off("length", provider="llamacpp", window=4096)[1]
    assert "-c 40960;" in problems.cut_off("length", provider="llamacpp", window=32768)[1]     # a quarter more, not twice as much
    none = problems.cut_off("length", provider="llamacpp")[1]                                   # a window nobody named
    assert "tokens)" not in none and "-c " in none
    # What the chat itself allows comes first, as for every provider.
    assert "100 output tokens this chat allows" in problems.cut_off("length", provider="llamacpp", max_tokens=100, output_tokens=100,
                                                                    window=8192)[1]
    assert problems.cut_off("stop", provider="llamacpp", window=8192) is None


# ---- Settings

async def test_settings_show_no_address_and_no_key_until_the_owner_gives_them(client):
    s = (await client.get("/api/settings")).json()
    assert s["llamacpp_url"] == "" and s["llamacpp_key_set"] is False
    assert llamacpp.KEY_ENV not in s["keys"]             # its key stands with its address, not among the cloud keys


@respx.mock
async def test_an_address_is_saved_as_llama_server_is_reached_and_the_model_is_in_the_list_at_once(client):
    listed()
    before = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert before["llamacpp"]["configured"] is False and before["llamacpp"]["models"] == []
    r = await client.put("/api/settings", json={"llamacpp_url": " 127.0.0.1:9931/v1 "})
    assert r.status_code == 200 and r.json()["llamacpp_url"] == AT
    assert config.env(llamacpp.URL_ENV) == AT
    assert f"{llamacpp.URL_ENV}={AT}" in config.env_file().read_text()
    now = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert now["llamacpp"]["configured"] is True and [m["id"] for m in now["llamacpp"]["models"]] == [QWEN]
    assert now["llamacpp"]["models"][0]["context_window"] == 8192 and now["llamacpp"]["models"][0]["supports_images"] is True


async def test_what_is_no_address_is_refused_with_the_reason_and_nothing_is_written(client):
    r = await client.put("/api/settings", json={"llamacpp_url": "ftp://127.0.0.1:9931", "max_turns": 7})
    assert r.status_code == 400 and r.json()["detail"] == "llama.cpp address: it has to begin with http:// or https://"
    s = (await client.get("/api/settings")).json()
    assert s["llamacpp_url"] == "" and s["max_turns"] != 7 and config.env(llamacpp.URL_ENV) is None


@respx.mock
async def test_an_emptied_address_switches_llama_cpp_off(client):
    listed()
    await client.put("/api/settings", json={"llamacpp_url": AT})
    r = await client.put("/api/settings", json={"llamacpp_url": ""})
    assert r.status_code == 200 and r.json()["llamacpp_url"] == "" and config.env(llamacpp.URL_ENV) is None
    entry = {p["name"]: p for p in (await client.get("/api/providers")).json()}["llamacpp"]
    assert entry["configured"] is False and entry["models"] == []


@respx.mock
async def test_the_address_of_one_server_is_saved_without_a_word_about_the_other(client):
    """colibri and llama.cpp each have an address: saving one leaves the other as it is."""
    r = await client.put("/api/settings", json={"colibri_url": "http://127.0.0.1:8000"})
    assert (r.json()["llamacpp_url"], r.json()["colibri_url"]) == ("", "http://127.0.0.1:8000")
    r = await client.put("/api/settings", json={"llamacpp_url": AT})
    assert (r.json()["llamacpp_url"], r.json()["colibri_url"]) == (AT, "http://127.0.0.1:8000")
    r = await client.put("/api/settings", json={"colibri_url": ""})
    assert (r.json()["llamacpp_url"], r.json()["colibri_url"]) == (AT, "")
    r = await client.put("/api/settings", json={"colibri_url": "127.0.0.1:8000", "llamacpp_url": ""})        # both in one request
    assert (r.json()["llamacpp_url"], r.json()["colibri_url"]) == ("", "http://127.0.0.1:8000")
    r = await client.put("/api/settings", json={"colibri_url": "ftp://x", "llamacpp_url": AT})                # one refused: neither is written
    assert r.status_code == 400 and r.json()["detail"].startswith("colibri address: ")
    assert (await client.get("/api/settings")).json()["llamacpp_url"] == ""


@respx.mock
async def test_the_key_is_saved_with_the_other_secrets_and_never_shown(client, at):
    route = listed()
    r = await client.put(f"/api/settings/keys/{llamacpp.KEY_ENV}", json={"value": "secret-key-1"})
    assert r.status_code == 204 and config.env(llamacpp.KEY_ENV) == "secret-key-1"
    text = (await client.get("/api/settings")).text
    assert json.loads(text)["llamacpp_key_set"] is True and "secret-key-1" not in text
    await client.get("/api/providers")
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key-1"


# ---- the list of models

@respx.mock
async def test_the_list_says_why_llama_cpp_offers_no_model(client, at):
    down()
    ps = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert ps["llamacpp"]["configured"] is True and ps["llamacpp"]["models"] == []
    assert ps["llamacpp"]["note"] == f"nothing answers at {AT}"


@respx.mock
async def test_a_router_puts_every_model_of_its_folder_in_the_list(client, at):
    listed(wire.ROUTER_NONE)
    ps = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert "note" not in ps["llamacpp"] and [m["id"] for m in ps["llamacpp"]["models"]] == ["story-a", "story-b"]


async def test_a_llama_cpp_that_is_not_set_up_is_not_asked_and_has_nothing_to_explain(client):
    with respx.mock(assert_all_called=False) as router:
        route = router.get(MODELS).mock(return_value=httpx.Response(200, text=wire.MODELS_QWEN))
        ps = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert route.call_count == 0 and ps["llamacpp"] == {"name": "llamacpp", "configured": False, "models": []}


# ---- what the page is told under the address

async def test_without_an_address_the_state_is_off(client, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    assert (await client.get("/api/system/llamacpp")).json() == {
        "url": "", "state": "off", "said": "", "router": False, "models": [], "tools": None, "overhead": 9000, "start_with": 20480}


@respx.mock
async def test_a_server_that_answers_is_described_with_its_model_its_context_and_its_template(client, at, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    listed(with_context(32768)), props(wire.PROPS_QWEN)
    assert (await client.get("/api/system/llamacpp")).json() == {
        "url": AT, "state": "ok", "said": "", "router": False, "tools": True, "overhead": 9000, "start_with": 20480,
        "models": [{"id": QWEN, "context": 32768, "images": True, "loaded": True, "failed": False, "enough": None}]}


@respx.mock
async def test_a_chat_template_without_tools_is_said_as_that(client, at, monkeypatch):
    """The story model has a plain template: llama-server then ignores the tools of a request without a word."""
    fixed_overhead(monkeypatch, 9000)
    listed(wire.MODELS_STORY), props(wire.PROPS_STORY)
    got = (await client.get("/api/system/llamacpp")).json()
    assert got["tools"] is False and got["models"][0]["id"] == STORY


@respx.mock
@pytest.mark.parametrize("answer", [httpx.Response(401, text=wire.NO_KEY), httpx.Response(200, text="<html>"),
                                    httpx.Response(200, text='{"chat_template_caps": {"supports_tools": "yes"}}'),
                                    httpx.Response(200, text='{"chat_template_caps": 3}'), httpx.Response(200, text="[1]")])
async def test_a_server_that_does_not_say_what_its_template_does_leaves_that_open(client, at, monkeypatch, answer):
    fixed_overhead(monkeypatch, 9000)
    listed()
    respx.get(PROPS).mock(return_value=answer)
    assert (await client.get("/api/system/llamacpp")).json()["tools"] is None


@respx.mock
@pytest.mark.parametrize("tokens, context_now, enough", [(9000, 16384, 20480), (30000, 32768, 40960), (9000, 32768, None),
                                                         (9000, 17192, None), (9000, 17191, 20480), (300000, 131072, 311296),
                                                         (1000, 8192, 12288)])
async def test_a_context_too_small_for_the_rules_and_tools_names_the_one_to_start_llama_server_with(client, at, monkeypatch, tokens,
                                                                                                   context_now, enough):
    """The rules and the tools of a new chat, and room for the chat itself (system.ROOM), have to fit."""
    assert system.ROOM == 8192
    fixed_overhead(monkeypatch, tokens)
    listed(with_context(context_now)), props()
    got = (await client.get("/api/system/llamacpp")).json()
    assert (got["models"][0]["context"], got["overhead"], got["models"][0]["enough"]) == (context_now, tokens, enough)


@respx.mock
async def test_a_router_is_described_with_each_model_and_is_not_asked_for_one_template(client, at, monkeypatch):
    fixed_overhead(monkeypatch, 100)
    listed(wire.ROUTER_A)
    asked = props(wire.ROUTER_PROPS)
    got = (await client.get("/api/system/llamacpp")).json()
    assert got["router"] is True and got["tools"] is None and asked.call_count == 0
    assert got["models"] == [{"id": "story-a", "context": 256, "images": False, "loaded": True, "failed": False, "enough": 12288},
                             {"id": "story-b", "context": 256, "images": False, "loaded": False, "failed": False, "enough": 12288}]
    assert (got["overhead"], got["start_with"]) == (100, 12288)


def named(name: str, **more) -> dict:
    """One model of a router's folder, not loaded, started with -c 256."""
    return {**router_entry(["--ctx-size", "256"], **more), "id": name}


@respx.mock
async def test_a_model_the_router_could_not_load_and_one_whose_context_is_not_known_are_said_as_that(client, at, monkeypatch):
    fixed_overhead(monkeypatch, 100)
    unknown = {**router_entry([]), "id": "unknown"}                 # no -c in its command: it takes what fits when it loads
    listed(a_list(named("broke", failed=True, exit_code=1), unknown))
    got = (await client.get("/api/system/llamacpp")).json()
    assert got["models"] == [{"id": "broke", "context": 256, "images": False, "loaded": False, "failed": True, "enough": 12288},
                             {"id": "unknown", "context": None, "images": False, "loaded": False, "failed": False, "enough": None}]


@respx.mock
async def test_a_server_that_does_not_answer_is_loading_or_wants_a_key_is_said_as_that(client, at, monkeypatch):
    fixed_overhead(monkeypatch, 30000)
    down()
    got = (await client.get("/api/system/llamacpp")).json()
    assert (got["url"], got["state"], got["said"], got["models"], got["tools"]) == (AT, "down", f"nothing answers at {AT}", [], None)
    assert (got["overhead"], got["start_with"]) == (30000, 40960)      # known without the server: what to start it with
    listed(wire.LOADING, 503)
    got = (await client.get("/api/system/llamacpp")).json()
    assert (got["state"], got["said"]) == ("loading", "it is still loading its model")
    listed(wire.NO_KEY, 401)
    got = (await client.get("/api/system/llamacpp")).json()
    assert got["state"] == "key" and "asks for a key" in got["said"]
    monkeypatch.setenv(llamacpp.KEY_ENV, "another-key")
    assert "did not accept the key" in (await client.get("/api/system/llamacpp")).json()["said"]


@respx.mock
async def test_looking_at_llama_server_also_makes_the_list_of_models_anew(client, at):
    down()
    assert {p["name"]: p for p in (await client.get("/api/providers")).json()}["llamacpp"]["models"] == []
    listed(), props()                                     # the owner started it, and looks at its line in Settings
    assert (await client.get("/api/system/llamacpp")).json()["state"] == "ok"
    assert [m["id"] for m in {p["name"]: p for p in (await client.get("/api/providers")).json()}["llamacpp"]["models"]] == [QWEN]


# ---- the setup check

def nothing_else(monkeypatch):
    async def fetch(url: str):
        return None
    monkeypatch.setattr(system, "_fetch", fetch)
    monkeypatch.setattr("hunterai.providers.codex.shutil.which", lambda *a, **k: None)
    monkeypatch.delenv("HUNTERAI_MOCK")


@respx.mock
async def test_a_llama_server_that_serves_a_model_is_a_model_to_talk_to(client, at, monkeypatch):
    nothing_else(monkeypatch)
    listed()
    got = (await client.get("/api/system/checkup")).json()
    items = {i["id"]: i for i in got["items"]}
    assert got["ready"] is True and f"llama.cpp: {QWEN}" in items["model"]["detail"] and "ready:" not in items["model"]["detail"]
    assert items["llamacpp"]["ok"] and items["llamacpp"]["detail"] == f"serves {QWEN} at {AT}" and not items["llamacpp"]["required"]


@respx.mock
async def test_a_router_is_a_model_to_talk_to_and_says_how_many_it_has(client, at, monkeypatch):
    nothing_else(monkeypatch)
    listed(wire.ROUTER_A)
    items = {i["id"]: i for i in (await client.get("/api/system/checkup")).json()["items"]}
    assert "llama.cpp: story-a, story-b" in items["model"]["detail"]
    assert items["llamacpp"]["ok"] and items["llamacpp"]["detail"] == f"has 2 models at {AT} and loads the one a chat asks for (loaded now: story-a)"


@respx.mock
async def test_a_llama_server_that_does_not_answer_is_no_model_to_talk_to_and_says_how_to_start_it(client, at, monkeypatch):
    nothing_else(monkeypatch)
    down()
    got = (await client.get("/api/system/checkup")).json()
    items = {i["id"]: i for i in got["items"]}
    assert got["ready"] is False and "llama.cpp" not in items["model"]["detail"]
    assert not items["llamacpp"]["ok"] and items["llamacpp"]["detail"] == f"nothing answers at {AT}"
    assert "Start llama-server" in items["llamacpp"]["fix"] and "Settings" in items["llamacpp"]["fix"]


@respx.mock
@pytest.mark.parametrize("names, among, detail", [
    (["a"], "llama.cpp: a", "has 1 model at {at} and loads the one a chat asks for (none loaded now)"),
    (["a", "b", "c", "d"], "llama.cpp: a, b, c, d", "has 4 models at {at} and loads the one a chat asks for (none loaded now)"),
    (["a", "b", "c", "d", "e", "f"], "llama.cpp: a, b, c, d and 2 more", "has 6 models at {at} and loads the one a chat asks for (none loaded now)"),
])
async def test_the_setup_check_names_four_models_of_a_router_and_counts_the_rest(client, at, monkeypatch, names, among, detail):
    nothing_else(monkeypatch)
    listed(a_list(*[named(n) for n in names]))
    items = {i["id"]: i for i in (await client.get("/api/system/checkup")).json()["items"]}
    assert items["model"]["detail"] == among and items["llamacpp"]["detail"] == detail.format(at=AT)


@respx.mock
async def test_the_line_of_llama_cpp_stands_after_ollama_and_after_colibri_when_that_has_an_address(client, at, monkeypatch):
    nothing_else(monkeypatch)
    listed()
    ids = [i["id"] for i in (await client.get("/api/system/checkup")).json()["items"]]
    assert ids[:3] == ["model", "ollama", "llamacpp"]
    monkeypatch.setenv("HUNTERAI_COLIBRI_URL", "http://127.0.0.1:8000")
    respx.route(url__startswith="http://127.0.0.1:8000").mock(side_effect=httpx.ConnectError("refused"))
    ids = [i["id"] for i in (await client.get("/api/system/checkup")).json()["items"]]
    assert ids[:4] == ["model", "ollama", "colibri", "llamacpp"]


async def test_without_an_address_the_setup_check_does_not_speak_of_llama_cpp(client, monkeypatch):
    nothing_else(monkeypatch)
    items = {i["id"]: i for i in (await client.get("/api/system/checkup")).json()["items"]}
    assert "llamacpp" not in items and "llama.cpp" not in json.dumps(items["model"])


# ---- scripts/smoke.sh

def no_server():
    respx.route(url__startswith=SERVER).mock(side_effect=httpx.ConnectError("refused"))


async def test_the_smoke_check_skips_a_llama_cpp_that_is_not_set_up(conn):
    code, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp") == [("skip", "no address is set for it (Settings, llama.cpp)")] and code == 0


@respx.mock
async def test_the_smoke_check_names_what_llama_server_serves_and_measures_its_context(conn, at, monkeypatch):
    no_server()
    fixed_overhead(monkeypatch, 9000)
    listed(with_context(32768)), props()
    code, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp") == [
        ("ok", f"{QWEN} at {AT}, a context of 32,768 tokens, reads pictures"),
        ("ok", "its chat template has tool calls"),
        ("ok", "a new chat sends about 9,000 tokens of rules and tools, which leaves room for the chat"),
        ("skip", "not called: run with --calls to send it one short request")]
    assert code == 0


@respx.mock
async def test_the_smoke_check_fails_a_template_without_tools_and_a_context_that_leaves_no_room(conn, at, monkeypatch):
    no_server()
    fixed_overhead(monkeypatch, 12000)
    listed(with_context(16384, wire.MODELS_STORY)), props(wire.PROPS_STORY)
    code, lines = await smoke_run(conn, "llamacpp")
    verdicts = smoke_said(lines, "llama.cpp")
    assert verdicts[0] == ("ok", f"{STORY} at {AT}, a context of 16,384 tokens, reads text only")
    assert verdicts[1] == ("FAIL", ("its chat template has no tool calls, so the model cannot use HunterAI's tools: start llama-server "
                                    "with a model whose template has them, or give it one (--chat-template-file)"))
    assert verdicts[2] == ("FAIL", ("a new chat sends about 12,000 tokens of rules and tools, which leaves too little of the context "
                                    "for the chat: start llama-server with a larger one (-c 20480; a larger context needs more memory)"))
    assert code == 1


@respx.mock
async def test_the_smoke_check_of_a_router_names_its_models_and_calls_none_that_is_not_loaded(conn, at, monkeypatch):
    no_server()
    fixed_overhead(monkeypatch, 100)
    listed(wire.ROUTER_NONE)
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.PLAIN))
    code, lines = await smoke_run(conn, "llamacpp", calls=True)
    verdicts = smoke_said(lines, "llama.cpp")
    assert verdicts[0] == ("ok", f"a router at {AT} with 2 models, none loaded now: story-a, story-b")
    assert verdicts[1] == ("FAIL", ("a new chat sends about 100 tokens of rules and tools, which leaves too little of the context for "
                                    "the chat with story-a, story-b: start llama-server with a larger one (-c 12288; a larger context "
                                    "needs more memory)"))
    assert verdicts[-1] == ("skip", "not called: no model is loaded, and a call would load one") and route.call_count == 0
    assert code == 1


@respx.mock
@pytest.mark.parametrize("names, said", [
    (["a"], "a router at {at} with 1 model, none loaded now: a"),
    (["a", "b", "c", "d", "e", "f"], "a router at {at} with 6 models, none loaded now: a, b, c, d, e, f"),
    (["a", "b", "c", "d", "e", "f", "g", "h"], "a router at {at} with 8 models, none loaded now: a, b, c, d, e, f and 2 more"),
])
async def test_the_smoke_check_names_six_models_of_a_router_and_counts_the_rest(conn, at, monkeypatch, names, said):
    no_server()
    fixed_overhead(monkeypatch, 100)
    listed(a_list(*[named(n) for n in names]))
    _, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp")[0] == ("ok", said.format(at=AT))


@respx.mock
async def test_the_smoke_check_says_which_models_of_a_router_are_loaded(conn, at, monkeypatch):
    no_server()
    fixed_overhead(monkeypatch, 100)
    listed(wire.ROUTER_A)
    _, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp")[0] == ("ok", f"a router at {AT} with 2 models, loaded now: story-a: story-a, story-b")


@respx.mock
async def test_the_smoke_check_says_nothing_of_a_template_or_a_context_the_server_does_not_tell(conn, at, monkeypatch):
    """An older llama-server: its list has no context, and its /props says neither that nor what the template does."""
    no_server()
    fixed_overhead(monkeypatch, 9000)
    bare = {k: v for k, v in entries(wire.MODELS_QWEN)[0].items() if k != "meta"}
    listed(a_list(bare)), props("{}")
    code, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp") == [
        ("ok", f"{QWEN} at {AT}, a context it does not name, reads pictures"),
        ("skip", "not called: run with --calls to send it one short request")]
    assert code == 0


@respx.mock
async def test_a_context_that_holds_the_rules_and_tools_and_just_the_room_for_a_chat_is_enough(conn, at, monkeypatch):
    no_server()
    fixed_overhead(monkeypatch, 9000)
    listed(with_context(9000 + system.ROOM)), props()
    code, lines = await smoke_run(conn, "llamacpp")
    assert ("ok", "a new chat sends about 9,000 tokens of rules and tools, which leaves room for the chat") in smoke_said(lines, "llama.cpp")
    assert code == 0
    listed(with_context(9000 + system.ROOM - 1))
    code, lines = await smoke_run(conn, "llamacpp")
    assert code == 1


@respx.mock
async def test_a_request_of_the_smoke_check_that_fails_is_said_in_the_words_for_llama_server(conn, at, monkeypatch):
    no_server()
    fixed_overhead(monkeypatch, 9000)
    listed(with_context(32768)), props()
    respx.post(CHAT).mock(return_value=httpx.Response(500, text='{"error":{"code":500,"message":"Compute error.","type":"server_error"}}'))
    code, lines = await smoke_run(conn, "llamacpp", calls=True)
    verdict, words = smoke_said(lines, "llama.cpp")[-1]
    assert verdict == "FAIL" and words.startswith("llama.cpp: ") and "Compute error." in words
    assert "the terminal it runs in says why" in words and code == 1


@respx.mock
async def test_the_smoke_check_sends_llama_server_one_short_request_when_asked(conn, at, monkeypatch):
    no_server()
    fixed_overhead(monkeypatch, 9000)
    listed(with_context(32768)), props()
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.PLAIN))
    code, lines = await smoke_run(conn, "llamacpp", calls=True)
    verdict, words = smoke_said(lines, "llama.cpp")[-1]
    assert verdict == "ok" and words.startswith(f"{QWEN} answered in ") and words.endswith("s (39 tokens in, 2 out)")
    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == 16 and sent["reasoning_effort"] == "none" and code == 0      # the tokens are for the word
    respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.KILLED))
    code, lines = await smoke_run(conn, "llamacpp", calls=True)
    verdict, words = smoke_said(lines, "llama.cpp")[-1]
    assert verdict == "FAIL" and "without finishing" in words and code == 1


@respx.mock
async def test_the_smoke_check_does_not_wake_a_server_that_sleeps(conn, at, monkeypatch):
    """Started with --sleep-idle-seconds, the model is out of memory until the next request: a call would load it."""
    no_server()
    fixed_overhead(monkeypatch, 9000)
    listed(with_context(32768)), props(json.dumps({**json.loads(wire.PROPS_QWEN), "is_sleeping": True}))
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.PLAIN))
    code, lines = await smoke_run(conn, "llamacpp", calls=True)
    verdicts = smoke_said(lines, "llama.cpp")
    assert verdicts[0] == ("ok", f"{QWEN} at {AT}, a context of 32,768 tokens, reads pictures")
    assert verdicts[1] == ("note", ("it put its model out of memory for being idle (--sleep-idle-seconds): the next request "
                                    "loads it again"))
    assert verdicts[-1] == ("skip", "not called: no model is loaded, and a call would load one")
    assert route.call_count == 0 and code == 0


@respx.mock
async def test_the_smoke_check_tells_a_llama_server_that_is_down_from_one_that_is_wrongly_set(conn, at, monkeypatch):
    down()
    code, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp") == [("note", f"nothing answers at {AT}: start llama-server when you want to use it")]
    assert code == 0
    await settings.put(conn, "default_provider", "llamacpp")         # new chats use it: then it is missed
    code, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp") == [("FAIL", f"nothing answers at {AT}, and new chats are set to use it: start llama-server")]
    assert code == 1
    await settings.put(conn, "default_provider", "abliteration")
    listed(wire.LOADING, 503)
    code, lines = await smoke_run(conn, "llamacpp")
    assert smoke_said(lines, "llama.cpp") == [("note", "it is still loading its model: run the check again in a moment")] and code == 0
    listed(wire.NO_KEY, 401)                               # something answers, and not as it should: that is a fault of the setup
    code, lines = await smoke_run(conn, "llamacpp")
    verdict, words = smoke_said(lines, "llama.cpp")[0]
    assert verdict == "FAIL" and "asks for a key" in words and code == 1


def test_llama_cpp_is_not_counted_among_the_cloud_providers(at):
    assert "llamacpp" not in [p.name for p in smoke._cloud_providers()]
    assert "llamacpp" in smoke.CHECKS


# ---- a local model: it works alone, and the heat guard knows it

def test_a_model_of_llama_cpp_works_alone_like_every_local_model():
    assert "llamacpp" in subagents.LOCAL_PROVIDERS and "colibri" in subagents.LOCAL_PROVIDERS and "ollama" in subagents.LOCAL_PROVIDERS


def test_a_llama_server_on_this_machine_heats_it_and_one_elsewhere_does_not(monkeypatch):
    assert cooling.here("llamacpp", QWEN) is False            # no address: nothing runs
    monkeypatch.setenv(llamacpp.URL_ENV, AT)
    assert cooling.here("llamacpp", QWEN) is True
    monkeypatch.setenv(llamacpp.URL_ENV, "http://localhost:9931")
    assert cooling.here("llamacpp", QWEN) is True
    monkeypatch.setenv(llamacpp.URL_ENV, "http://192.168.1.20:9931")
    assert cooling.here("llamacpp", QWEN) is False
    assert cooling.here("openai", "gpt-5") is False


@respx.mock
async def test_a_call_loads_a_model_the_router_does_not_hold_and_not_one_that_is_in_memory(at):
    listed(), props()
    assert await cooling.loads("llamacpp", QWEN) is False                  # one model, held since the server started
    registry._instances.clear()
    listed(wire.ROUTER_A)
    assert await cooling.loads("llamacpp", "story-a") is False
    assert await cooling.loads("llamacpp", "story-b") is True
    assert await cooling.loads("llamacpp", "story-c") is True              # nobody says it is in memory
    registry._instances.clear()
    down()
    assert await cooling.loads("llamacpp", QWEN) is True


@respx.mock
async def test_a_call_to_a_sleeping_server_loads_its_model(at):
    """Started with --sleep-idle-seconds it frees the memory when idle; its list still names the model."""
    listed(wire.MODELS_ASLEEP), props(wire.PROPS_ASLEEP)
    assert await cooling.loads("llamacpp", STORY) is True
    registry._instances.clear()
    listed(wire.MODELS_STORY), props(wire.PROPS_STORY)
    assert await cooling.loads("llamacpp", STORY) is False


# ---- a chat that llama.cpp answers

async def no_bytes(upload_id):
    return b""


async def go(conn, chat, user):
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    return await runs.get(conn, run_id), await messages.get(conn, mid)


async def a_chat(conn, text="hi", model=QWEN):
    chat = await chats.create(conn, None, "pentesting", "llamacpp", model, {})
    return chat, await messages.add(conn, chat.id, None, "user", [TextBlock(text=text)])


@respx.mock
async def test_a_chat_set_to_llama_cpp_is_answered_by_it(conn, at):
    listed()
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.PLAIN))
    run, msg = await go(conn, *await a_chat(conn))
    assert (run.status, run.error) == ("done", None)
    assert (msg.provider, msg.model, msg.blocks[-1].text) == ("llamacpp", QWEN, "5")
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == QWEN and body["messages"][-1] == {"role": "user", "content": "hi"}


@respx.mock
async def test_an_answer_llama_server_cut_off_says_so_under_it(conn, at):
    listed()
    respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.AT_LIMIT))
    run, msg = await go(conn, *await a_chat(conn))
    assert (run.status, run.error) == ("done", None)
    assert [b.type for b in msg.blocks] == ["text", "notice"] and msg.blocks[-1].kind == "cut_off"
    assert "8,192 tokens" in msg.blocks[-1].text and "-c 12288;" in msg.blocks[-1].text


@respx.mock
async def test_an_answer_llama_server_did_not_finish_is_an_error_and_what_came_is_kept(conn, at):
    listed(wire.MODELS_STORY)
    respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.KILLED))
    run, msg = await go(conn, *await a_chat(conn, model=STORY))
    assert run.status == "error" and run.error.startswith(f"llama-server at {AT} ended the answer without finishing it")
    assert run.error.count("Start it again") == 1                         # its own advice, and no second one
    assert [b.text for b in msg.blocks if b.type == "text"] == ["M"]


@respx.mock
async def test_a_refusal_by_llama_server_for_size_is_answered_by_a_smaller_request(conn, at):
    """HunterAI counts characters and llama-server counts tokens: when it says the request does not fit after all, the
    chat is shortened and sent once more."""
    listed(with_context(32768))
    refused = []

    def reply(request):
        body = json.loads(request.content)
        summary = body["messages"][0]["content"] == context.SUMMARY_SYSTEM
        if not summary and not refused:
            refused.append(len(request.content))
            return httpx.Response(400, text=wire.TOO_LONG)
        return httpx.Response(200, text=chunks("The early part, in short." if summary else "A smaller request was answered."))
    route = respx.post(CHAT).mock(side_effect=reply)
    chat = await chats.create(conn, None, "pentesting", "llamacpp", QWEN, {})
    parent = None
    for n in range(1, 5):
        user = await messages.add(conn, chat.id, parent, "user", [TextBlock(text=f"question {n}: " + "words " * 500)])
        parent = (await messages.add(conn, chat.id, user.id, "assistant", [TextBlock(text=f"answer {n}: " + "found " * 500)],
                                     provider="llamacpp", model=QWEN)).id
    run, msg = await go(conn, chat, await messages.add(conn, chat.id, parent, "user", [TextBlock(text="and now?")]))
    assert (run.status, msg.blocks[-1].text) == ("done", "A smaller request was answered.")
    answers = [c.request for c in route.calls if json.loads(c.request.content)["messages"][0]["content"] != context.SUMMARY_SYSTEM]
    assert len(answers) == 2 and len(answers[1].content) < refused[0] / 1.5
    assert json.loads(answers[1].content)["messages"][-1] == {"role": "user", "content": "and now?"}


@respx.mock
async def test_a_request_llama_server_cannot_hold_at_all_says_what_to_start_it_with(conn, at):
    listed()
    respx.post(CHAT).mock(return_value=httpx.Response(400, text=wire.TOO_LONG))
    run, _ = await go(conn, *await a_chat(conn))
    assert run.status == "error" and "1,841 tokens for a window of 256" in run.error
    assert "-c 4096" in run.error and run.error.rstrip().endswith(context.TOO_LONG.strip())


@respx.mock
async def test_a_chat_set_to_llama_cpp_while_it_is_down_says_to_start_it(conn, at, monkeypatch):
    monkeypatch.setattr(engine, "RETRY_PAUSE", 0)
    down()
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    run, _ = await go(conn, *await a_chat(conn))
    assert run.status == "error" and run.error.startswith(f"llama.cpp does not answer at {AT} (ConnectError). ")
    assert "Start llama-server" in run.error


# ---- the line of loaded models at the bottom of the page

@respx.mock
async def test_the_machine_state_names_the_models_llama_server_holds(client, at, monkeypatch):
    nothing_else(monkeypatch)
    listed()
    awake = props()
    assert (await client.get("/api/system/status")).json()["llamacpp"] == [{"model": QWEN, "context": 8192}]
    registry._instances.clear()
    listed(wire.ROUTER_A)                                   # of a router's models, the ones in memory: its list says which
    assert (await client.get("/api/system/status")).json()["llamacpp"] == [{"model": "story-a", "context": 256}]
    registry._instances.clear()
    listed(wire.ROUTER_NONE)
    assert (await client.get("/api/system/status")).json()["llamacpp"] == []
    assert awake.call_count == 1                            # a router is not asked for the properties of one model


@respx.mock
async def test_a_model_a_sleeping_server_put_out_of_memory_is_not_named_and_the_card_is_left_alone(client, at, monkeypatch):
    """Started with --sleep-idle-seconds, llama-server frees the memory when idle, and its list names the model all the
    same. Named as held, the card would be asked for its temperature every few seconds, which keeps a laptop's card
    awake for a model that is not there."""
    nothing_else(monkeypatch)
    asked = []

    async def card():
        asked.append(1)
        return 64.0
    monkeypatch.setattr(system.thermal, "gpu_temp", card)
    listed(wire.MODELS_ASLEEP), props(wire.PROPS_ASLEEP)
    said = (await client.get("/api/system/status")).json()
    assert (said["llamacpp"], said["gpu_temp"], asked) == ([], None, [])
    registry._instances.clear()
    listed(wire.MODELS_STORY), props(wire.PROPS_STORY)      # a request woke it: the model is in memory again
    said = (await client.get("/api/system/status")).json()
    assert (said["llamacpp"], said["gpu_temp"], asked) == ([{"model": STORY, "context": 256}], 64.0, [1])


@respx.mock
async def test_a_server_that_does_not_say_whether_it_sleeps_holds_its_model(client, at, monkeypatch):
    """An older llama-server, or one whose /props cannot be had: what its list names is taken as loaded."""
    nothing_else(monkeypatch)
    listed(), props("not json", 500)
    assert (await client.get("/api/system/status")).json()["llamacpp"] == [{"model": QWEN, "context": 8192}]


@respx.mock
async def test_the_machine_state_names_no_llama_cpp_model_where_none_is_held(client, monkeypatch):
    nothing_else(monkeypatch)
    asked = listed()
    assert (await client.get("/api/system/status")).json()["llamacpp"] is None and not asked.called      # no address: nothing is asked
    monkeypatch.setenv(llamacpp.URL_ENV, AT)
    down()
    assert (await client.get("/api/system/status")).json()["llamacpp"] is None                             # nothing answers there
