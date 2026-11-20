"""colibri in the app: its address and key in Settings, what the page and the checks say about it, the words for
its errors, and a chat that is answered by it. The server is a stand-in that sends what colibri's own server sent
(tests/colibri_wire.py)."""
import json

import httpx
import pytest
import respx

from hunterai import config, context, db, engine, problems, smoke
from hunterai.api import system
from hunterai.providers import colibri
from hunterai.providers.base import ProviderError
from hunterai.repo import chats, messages, runs, settings
from hunterai.types import TextBlock
from tests import colibri_wire as wire
from tests.test_api import client  # noqa: F401 (client is a fixture)
from tests.test_smoke import run as smoke_run
from tests.test_smoke import said as smoke_said

AT = "http://127.0.0.1:8000"
MODELS = AT + "/v1/models"
CHAT = AT + "/v1/chat/completions"
SERVER = "http://127.0.0.1:8790"
GLM, QWEN = "glm-5.2-colibri", "qwen3.6-colibri"


@pytest.fixture
def at(monkeypatch):
    monkeypatch.setenv(colibri.URL_ENV, AT)


def listed(text=wire.MODELS_GLM, status=200):
    return respx.get(MODELS).mock(return_value=httpx.Response(status, text=text, headers={"content-type": "application/json"}))


def down():
    return respx.get(MODELS).mock(side_effect=httpx.ConnectError("refused"))


def with_context(tokens: int) -> str:
    entry = {**json.loads(wire.MODELS_GLM)["data"][0], "context_length": tokens, "max_model_len": tokens}
    return json.dumps({"object": "list", "data": [entry]})


def chunks(text: str, ended: str = "stop") -> str:
    """An answer in the chunks colibri writes."""
    def one(delta, finish=None):
        return "data: " + json.dumps({"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": GLM,
                                      "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
                                      "usage": None}) + "\n\n"
    usage = "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 9, "total_tokens": 59}}) + "\n\n"
    return one({"role": "assistant", "content": ""}) + one({"content": text}) + one({}, ended) + usage + "data: [DONE]\n\n"


def failed(kind, message, status=None, complete=False) -> ProviderError:
    e = ProviderError(kind, message, status, complete=complete)
    e.provider, e.model = "colibri", GLM
    return e


# ---- its errors, in words

def test_a_colibri_that_does_not_answer_is_told_how_to_start_it():
    said = problems.model_call(failed("unavailable", f"colibri does not answer at {AT} (ConnectError)"))
    assert said.startswith(f"colibri does not answer at {AT} (ConnectError). ")
    assert "./start-here.sh in the colibri folder" in said and "Settings" in said and "internet" not in said


def test_a_failure_colibri_answered_with_is_not_told_to_start_what_runs():
    said = problems.model_call(failed("unavailable", "HTTP 503: The inference scheduler is shutting down.", 503))
    assert said.startswith("colibri: HTTP 503: The inference scheduler is shutting down. ")
    assert "start-here" not in said and "c/coli logs" in said and "send the message again" in said


def test_a_colibri_that_sent_nothing_in_time_is_said_to_be_busy():
    said = problems.model_call(failed("timeout", "colibri timed out"))
    assert "one request at a time" in said and "provider is slow or down" not in said


def test_what_colibri_did_not_accept_points_at_the_model_it_serves_and_not_at_another_in_the_list():
    said = problems.model_call(failed("bad_request", "HTTP 400: Tool use is not wired up for the OLMoE engine yet.", 400))
    assert said.startswith("colibri: HTTP 400: Tool use is not wired up for the OLMoE engine yet. ")
    assert "serve another" in said and "--reconfigure" in said and "pick another one" not in said


def test_the_other_providers_keep_their_words():
    e = ProviderError("unavailable", "openai unreachable: ConnectError")
    e.provider = "openai"
    assert "check the internet connection" in problems.model_call(e)
    e = ProviderError("unavailable", "Ollama not reachable at http://127.0.0.1:11434 (ConnectError)")
    e.provider = "ollama"
    assert "ollama serve" in problems.model_call(e)


def test_an_answer_colibri_cut_off_names_its_window_and_how_to_start_it_with_a_larger_one():
    kind, text = problems.cut_off("length", provider="colibri", window=32768)
    assert kind == "cut_off" and "32,768 tokens" in text and "./start-here.sh --ctx 65536" in text
    assert "the request and the answer" in text and "Context window for local models" not in text
    assert "--ctx" not in problems.cut_off("length", provider="colibri", window=262144)[1]       # there is no larger one to name
    assert "tokens)" not in problems.cut_off("length", provider="colibri")[1]                    # a window nobody named
    # What the chat itself allows comes first, as for every provider.
    assert "100 output tokens this chat allows" in problems.cut_off("length", provider="colibri", max_tokens=100, output_tokens=100,
                                                                    window=32768)[1]
    assert problems.cut_off("stop", provider="colibri", window=32768) is None


# ---- Settings

async def test_settings_show_no_address_and_no_key_until_the_owner_gives_them(client):
    s = (await client.get("/api/settings")).json()
    assert s["colibri_url"] == "" and s["colibri_key_set"] is False
    assert colibri.KEY_ENV not in s["keys"]              # its key stands with its address, not among the cloud keys


@respx.mock
async def test_an_address_is_saved_as_colibri_is_reached_and_the_model_is_in_the_list_at_once(client):
    listed()
    before = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert before["colibri"]["configured"] is False and before["colibri"]["models"] == []
    r = await client.put("/api/settings", json={"colibri_url": " 127.0.0.1:8000/v1 "})
    assert r.status_code == 200 and r.json()["colibri_url"] == AT
    assert config.env(colibri.URL_ENV) == AT
    assert f"{colibri.URL_ENV}={AT}" in config.env_file().read_text()
    now = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert now["colibri"]["configured"] is True and [m["id"] for m in now["colibri"]["models"]] == [GLM]
    assert now["colibri"]["models"][0]["context_window"] == 32768


async def test_what_is_no_address_is_refused_with_the_reason_and_nothing_is_written(client):
    r = await client.put("/api/settings", json={"colibri_url": "ftp://127.0.0.1:8000", "max_turns": 7})
    assert r.status_code == 400 and r.json()["detail"] == "colibri address: it has to begin with http:// or https://"
    s = (await client.get("/api/settings")).json()
    assert s["colibri_url"] == "" and s["max_turns"] != 7 and config.env(colibri.URL_ENV) is None


@respx.mock
async def test_an_emptied_address_switches_colibri_off(client):
    listed()
    await client.put("/api/settings", json={"colibri_url": AT})
    r = await client.put("/api/settings", json={"colibri_url": ""})
    assert r.status_code == 200 and r.json()["colibri_url"] == "" and config.env(colibri.URL_ENV) is None
    entry = {p["name"]: p for p in (await client.get("/api/providers")).json()}["colibri"]
    assert entry["configured"] is False and entry["models"] == []


@respx.mock
async def test_the_key_is_saved_with_the_other_secrets_and_never_shown(client, at):
    route = listed()
    r = await client.put(f"/api/settings/keys/{colibri.KEY_ENV}", json={"value": "secret-key-1"})
    assert r.status_code == 204 and config.env(colibri.KEY_ENV) == "secret-key-1"
    text = (await client.get("/api/settings")).text
    assert json.loads(text)["colibri_key_set"] is True and "secret-key-1" not in text
    await client.get("/api/providers")
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-key-1"


# ---- the list of models

@respx.mock
async def test_the_list_says_why_colibri_offers_no_model(client, at):
    down()
    ps = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert ps["colibri"]["configured"] is True and ps["colibri"]["models"] == []
    assert ps["colibri"]["note"] == f"nothing answers at {AT}"
    assert "note" not in ps["mock"] and "note" not in ps["ollama"]


@respx.mock
async def test_a_colibri_that_serves_a_model_has_nothing_to_explain(client, at):
    listed()
    ps = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert "note" not in ps["colibri"] and ps["colibri"]["models"][0]["id"] == GLM


async def test_a_note_stands_in_place_of_models_and_never_beside_them(client):
    from hunterai.providers import registry
    from hunterai.providers.base import ModelInfo

    class Talkative:
        name = "colibri"

        def __init__(self, models):
            self.listed = models

        def configured(self):
            return True

        async def models(self):
            return self.listed

        def note(self):
            return "something to say"
    registry.override("colibri", Talkative([ModelInfo(id="m1", label="m1")]))
    entry = {p["name"]: p for p in (await client.get("/api/providers")).json()}["colibri"]
    assert [m["id"] for m in entry["models"]] == ["m1"] and "note" not in entry
    from hunterai.api import providers as providers_api
    providers_api.clear_cache()
    registry.override("colibri", Talkative([]))
    assert {p["name"]: p for p in (await client.get("/api/providers")).json()}["colibri"]["note"] == "something to say"


async def test_a_colibri_that_is_not_set_up_is_not_asked_and_has_nothing_to_explain(client):
    with respx.mock(assert_all_called=False) as router:
        route = router.get(MODELS).mock(return_value=httpx.Response(200, text=wire.MODELS_GLM))
        ps = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert route.call_count == 0 and ps["colibri"] == {"name": "colibri", "configured": False, "models": []}


# ---- what the page is told under the address

async def test_without_an_address_the_state_is_off(client):
    assert (await client.get("/api/system/colibri")).json() == {
        "url": "", "state": "off", "said": "", "model": None, "context": None, "images": False, "overhead": None, "enough": None}


@respx.mock
async def test_a_colibri_that_answers_is_described_with_its_model_and_its_context(client, at, monkeypatch):
    async def overhead(conn):
        return {"tokens": 9000, "tools": 20, "on_demand": 0, "window": 16384}
    monkeypatch.setattr(system, "overhead", overhead)
    listed(wire.MODELS_QWEN)
    assert (await client.get("/api/system/colibri")).json() == {
        "url": AT, "state": "ok", "said": "", "model": QWEN, "context": 65536, "images": True, "overhead": 9000, "enough": None}


@respx.mock
@pytest.mark.parametrize("tokens, context_now, enough", [(9000, 16384, 32768), (30000, 32768, 65536), (9000, 32768, None),
                                                         (300000, 131072, 262144)])
async def test_a_context_too_small_for_the_rules_and_tools_names_the_one_to_start_colibri_with(client, at, monkeypatch, tokens,
                                                                                              context_now, enough):
    """The rules and the tools of a new chat, and room for the chat itself (system.ROOM), have to fit."""
    async def overhead(conn):
        return {"tokens": tokens, "tools": 20, "on_demand": 0, "window": 16384}
    monkeypatch.setattr(system, "overhead", overhead)
    listed(with_context(context_now))
    got = (await client.get("/api/system/colibri")).json()
    assert (got["context"], got["overhead"], got["enough"]) == (context_now, tokens, enough)


@respx.mock
async def test_a_colibri_that_does_not_answer_or_wants_a_key_is_said_as_that(client, at, monkeypatch):
    down()
    got = (await client.get("/api/system/colibri")).json()
    assert (got["url"], got["state"], got["said"], got["model"]) == (AT, "down", f"nothing answers at {AT}", None)
    listed(wire.NO_KEY, 401)
    got = (await client.get("/api/system/colibri")).json()
    assert got["state"] == "key" and "asks for a key" in got["said"]
    monkeypatch.setenv(colibri.KEY_ENV, "another-key")
    assert "did not accept the key" in (await client.get("/api/system/colibri")).json()["said"]


@respx.mock
async def test_looking_at_colibri_also_makes_the_list_of_models_anew(client, at):
    down()
    assert {p["name"]: p for p in (await client.get("/api/providers")).json()}["colibri"]["models"] == []
    listed()                                              # the owner started it, and looks at its line in Settings
    assert (await client.get("/api/system/colibri")).json()["state"] == "ok"
    assert [m["id"] for m in {p["name"]: p for p in (await client.get("/api/providers")).json()}["colibri"]["models"]] == [GLM]


# ---- the setup check

def nothing_else(monkeypatch):
    async def fetch(url: str):
        return None
    monkeypatch.setattr(system, "_fetch", fetch)
    monkeypatch.setattr("hunterai.providers.codex.shutil.which", lambda *a, **k: None)
    monkeypatch.delenv("HUNTERAI_MOCK")


@respx.mock
async def test_a_colibri_that_serves_a_model_is_a_model_to_talk_to(client, at, monkeypatch):
    nothing_else(monkeypatch)
    listed()
    got = (await client.get("/api/system/checkup")).json()
    items = {i["id"]: i for i in got["items"]}
    assert got["ready"] is True and f"colibri: {GLM}" in items["model"]["detail"]
    assert items["colibri"]["ok"] and items["colibri"]["detail"] == f"serves {GLM} at {AT}" and not items["colibri"]["required"]


@respx.mock
async def test_a_colibri_that_does_not_answer_is_no_model_to_talk_to_and_says_how_to_start_it(client, at, monkeypatch):
    nothing_else(monkeypatch)
    down()
    got = (await client.get("/api/system/checkup")).json()
    items = {i["id"]: i for i in got["items"]}
    assert got["ready"] is False and "colibri" not in items["model"]["detail"]
    assert not items["colibri"]["ok"] and items["colibri"]["detail"] == f"nothing answers at {AT}"
    assert "./start-here.sh" in items["colibri"]["fix"] and "Settings" in items["colibri"]["fix"]


async def test_without_an_address_the_setup_check_does_not_speak_of_colibri(client, monkeypatch):
    nothing_else(monkeypatch)
    items = {i["id"]: i for i in (await client.get("/api/system/checkup")).json()["items"]}
    assert "colibri" not in items and "colibri" not in json.dumps(items["model"])


# ---- scripts/smoke.sh

def no_server():
    respx.route(url__startswith=SERVER).mock(side_effect=httpx.ConnectError("refused"))


async def test_the_smoke_check_skips_a_colibri_that_is_not_set_up(conn):
    code, lines = await smoke_run(conn, "colibri")
    assert smoke_said(lines, "colibri") == [("skip", "no address is set for it (Settings, colibri)")] and code == 0


@respx.mock
async def test_the_smoke_check_names_what_colibri_serves_and_measures_its_context(conn, at, monkeypatch):
    no_server()
    async def overhead(conn):
        return {"tokens": 9000, "tools": 20, "on_demand": 0, "window": 16384}
    monkeypatch.setattr(system, "overhead", overhead)
    listed(wire.MODELS_QWEN)
    code, lines = await smoke_run(conn, "colibri")
    assert smoke_said(lines, "colibri") == [
        ("ok", f"{QWEN} at {AT}, a context of 65,536 tokens, reads pictures"),
        ("ok", "a new chat sends about 9,000 tokens of rules and tools, which leaves room for the chat"),
        ("skip", "not called: run with --calls to send it one short request")]
    assert code == 0


@respx.mock
async def test_the_smoke_check_fails_a_context_that_leaves_no_room_for_the_chat(conn, at, monkeypatch):
    no_server()
    async def overhead(conn):
        return {"tokens": 12000, "tools": 20, "on_demand": 0, "window": 16384}
    monkeypatch.setattr(system, "overhead", overhead)
    listed(with_context(16384))
    code, lines = await smoke_run(conn, "colibri")
    verdicts = smoke_said(lines, "colibri")
    assert verdicts[0] == ("ok", f"{GLM} at {AT}, a context of 16,384 tokens, reads text only")
    assert verdicts[1] == ("FAIL", "a new chat sends about 12,000 tokens of rules and tools, which leaves too little of the context "
                                   "for the chat: start colibri with a larger one (./start-here.sh --ctx 32768 in the colibri folder; "
                                   "a larger context needs more memory)")
    assert code == 1


@respx.mock
async def test_the_smoke_check_says_so_when_no_context_colibri_offers_would_be_enough(conn, at, monkeypatch):
    no_server()
    async def overhead(conn):
        return {"tokens": 300000, "tools": 900, "on_demand": 0, "window": 16384}
    monkeypatch.setattr(system, "overhead", overhead)
    listed(with_context(262144))
    code, lines = await smoke_run(conn, "colibri")
    assert smoke_said(lines, "colibri")[1] == ("FAIL", "a new chat sends about 300,000 tokens of rules and tools, which leaves too little of "
                                                       "the context for the chat, and colibri offers no larger one: switch off the MCP "
                                                       "servers you do not need")
    assert code == 1


@respx.mock
async def test_the_smoke_check_sends_colibri_one_short_request_when_asked(conn, at, monkeypatch):
    no_server()
    async def overhead(conn):
        return {"tokens": 9000, "tools": 20, "on_demand": 0, "window": 16384}
    monkeypatch.setattr(system, "overhead", overhead)
    listed()
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.HELLO))
    code, lines = await smoke_run(conn, "colibri", calls=True)
    verdict, words = smoke_said(lines, "colibri")[-1]
    assert verdict == "ok" and words.startswith(f"{GLM} answered in ") and words.endswith("s (20 tokens in, 3 out)")
    sent = json.loads(route.calls.last.request.content)
    assert sent["max_tokens"] == 16 and code == 0
    assert sent["enable_thinking"] is False                # sixteen tokens are for the word, not for thinking about it
    respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.DIED))
    code, lines = await smoke_run(conn, "colibri", calls=True)
    verdict, words = smoke_said(lines, "colibri")[-1]
    assert verdict == "FAIL" and "without finishing" in words and code == 1


@respx.mock
async def test_the_smoke_check_tells_a_colibri_that_is_down_from_one_that_is_wrongly_set(conn, at, monkeypatch):
    down()
    code, lines = await smoke_run(conn, "colibri")
    assert smoke_said(lines, "colibri") == [("note", f"nothing answers at {AT}: start it when you want to use it (./start-here.sh in "
                                                     "the colibri folder)")]
    assert code == 0
    await settings.put(conn, "default_provider", "colibri")          # new chats use it: then it is missed
    code, lines = await smoke_run(conn, "colibri")
    assert smoke_said(lines, "colibri") == [("FAIL", f"nothing answers at {AT}, and new chats are set to use it: start it "
                                                     "(./start-here.sh in the colibri folder)")]
    assert code == 1
    await settings.put(conn, "default_provider", "abliteration")
    listed(wire.NO_KEY, 401)                               # something answers, and not as it should: that is a fault of the setup
    code, lines = await smoke_run(conn, "colibri")
    verdict, words = smoke_said(lines, "colibri")[0]
    assert verdict == "FAIL" and "asks for a key" in words and code == 1


def test_colibri_is_not_counted_among_the_cloud_providers(at):
    assert "colibri" not in [p.name for p in smoke._cloud_providers()]
    assert "colibri" in smoke.CHECKS


# ---- a chat that colibri answers

async def no_bytes(upload_id):
    return b""


async def go(conn, chat, user):
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    return await runs.get(conn, run_id), await messages.get(conn, mid)


async def a_chat(conn, text="hi"):
    chat = await chats.create(conn, None, "pentesting", "colibri", GLM, {})
    return chat, await messages.add(conn, chat.id, None, "user", [TextBlock(text=text)])


@respx.mock
async def test_a_chat_set_to_colibri_is_answered_by_it(conn, at):
    listed()
    route = respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.HELLO))
    run, msg = await go(conn, *await a_chat(conn))
    assert (run.status, run.error) == ("done", None)
    assert (msg.provider, msg.model, msg.blocks[-1].text) == ("colibri", GLM, "Hello from colibri.")
    body = json.loads(route.calls.last.request.content)
    assert body["model"] == GLM and body["messages"][-1] == {"role": "user", "content": "hi"}


@respx.mock
async def test_an_answer_colibri_cut_off_says_so_under_it(conn, at):
    listed()
    respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.AT_LIMIT))
    run, msg = await go(conn, *await a_chat(conn))
    assert (run.status, run.error) == ("done", None)
    assert [b.type for b in msg.blocks] == ["text", "notice"] and msg.blocks[-1].kind == "cut_off"
    assert "32,768 tokens" in msg.blocks[-1].text and "./start-here.sh --ctx 65536" in msg.blocks[-1].text


@respx.mock
async def test_an_answer_colibri_did_not_finish_is_an_error_and_what_came_is_kept(conn, at):
    listed()
    respx.post(CHAT).mock(return_value=httpx.Response(200, text=wire.DIED))
    run, msg = await go(conn, *await a_chat(conn))
    assert run.status == "error" and run.error.startswith("colibri ended the answer without finishing it")
    assert "c/coli logs" in run.error and run.error.count("colibri usually starts") == 1         # its own advice, and no second one
    assert [b.text for b in msg.blocks if b.type == "text"] == ["The first half of an answer"]


@respx.mock
async def test_a_refusal_by_colibri_for_size_is_answered_by_a_smaller_request(conn, at):
    """HunterAI counts characters and colibri counts tokens: when colibri says the request does not fit after all, the
    chat is shortened and sent once more."""
    listed()
    refused = []

    def reply(request):
        body = json.loads(request.content)
        summary = body["messages"][0]["content"] == context.SUMMARY_SYSTEM
        if not summary and not refused:
            refused.append(len(request.content))
            return httpx.Response(400, text=wire.TOO_LARGE)
        return httpx.Response(200, text=chunks("The early part, in short." if summary else "A smaller request was answered."))
    route = respx.post(CHAT).mock(side_effect=reply)
    chat = await chats.create(conn, None, "pentesting", "colibri", GLM, {})
    parent = None
    for n in range(1, 5):
        user = await messages.add(conn, chat.id, parent, "user", [TextBlock(text=f"question {n}: " + "words " * 500)])
        parent = (await messages.add(conn, chat.id, user.id, "assistant", [TextBlock(text=f"answer {n}: " + "found " * 500)],
                                     provider="colibri", model=GLM)).id
    run, msg = await go(conn, chat, await messages.add(conn, chat.id, parent, "user", [TextBlock(text="and now?")]))
    assert (run.status, msg.blocks[-1].text) == ("done", "A smaller request was answered.")
    answers = [c.request for c in route.calls if json.loads(c.request.content)["messages"][0]["content"] != context.SUMMARY_SYSTEM]
    assert len(answers) == 2 and len(answers[1].content) < refused[0] / 1.5
    assert json.loads(answers[1].content)["messages"][-1] == {"role": "user", "content": "and now?"}


@respx.mock
async def test_a_request_colibri_cannot_hold_at_all_says_what_to_start_it_with(conn, at):
    listed()
    respx.post(CHAT).mock(return_value=httpx.Response(400, text=wire.TOO_LARGE))
    run, _ = await go(conn, *await a_chat(conn))
    assert run.status == "error" and "at least 40,000 tokens for a window of 32,768" in run.error
    assert "./start-here.sh --ctx 65536" in run.error and run.error.rstrip().endswith(context.TOO_LONG.strip())


@respx.mock
async def test_a_chat_set_to_colibri_while_it_is_down_says_how_to_start_it(conn, at, monkeypatch):
    monkeypatch.setattr(engine, "RETRY_PAUSE", 0)
    down()
    respx.post(CHAT).mock(side_effect=httpx.ConnectError("refused"))
    run, _ = await go(conn, *await a_chat(conn))
    assert run.status == "error" and run.error.startswith(f"colibri does not answer at {AT} (ConnectError). ")
    assert "./start-here.sh in the colibri folder" in run.error


# ---- the line of loaded models at the bottom of the page

@respx.mock
async def test_the_machine_state_names_the_model_colibri_holds(client, at, monkeypatch):
    """The line asked Ollama alone, so a chat answered by colibri stood over "no local model loaded"."""
    nothing_else(monkeypatch)
    listed()
    assert (await client.get("/api/system/status")).json()["colibri"] == {"model": GLM, "context": 32768}


@respx.mock
async def test_the_machine_state_names_no_colibri_model_where_none_is_held(client, monkeypatch):
    nothing_else(monkeypatch)
    asked = listed()
    assert (await client.get("/api/system/status")).json()["colibri"] is None and not asked.called      # no address: nothing is asked
    monkeypatch.setenv(colibri.URL_ENV, AT)
    down()
    assert (await client.get("/api/system/status")).json()["colibri"] is None                             # nothing answers there
