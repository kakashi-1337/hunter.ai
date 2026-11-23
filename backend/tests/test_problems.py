"""An error says what is wrong and what to do about it, in words the owner can act on."""
import httpx
import pytest
from pydantic import ValidationError

from hunterai import config, engine, problems, stale
from hunterai.api import system
from hunterai.api.app import create_app
from hunterai.permissions import Policy
from hunterai.providers.base import ProviderError
from hunterai.providers.ollama import TOO_LARGE
from tests.test_api import BASE, client, new_chat, send  # noqa: F401 (client is a fixture)


def refused(**fields) -> list[dict]:
    with pytest.raises(ValidationError) as caught:
        Policy(**fields)
    return caught.value.errors()


# ---- input the server does not accept

def test_an_invalid_value_is_said_with_its_name_and_what_is_wrong_with_it():
    assert problems.invalid(refused(risk={"read": "maybe"})) == "\"risk.read\": input should be 'allow', 'ask' or 'deny'."
    assert problems.invalid(refused(read_paths=["relative/path"])) == '"read_paths": path must be absolute: relative/path.'


def test_a_setting_the_server_does_not_know_is_said_as_that():
    assert problems.invalid(refused(bogus=1)) == '"bogus" is not a setting this server knows.'


def test_an_unknown_setting_on_a_server_that_runs_old_code_says_to_restart_it():
    """The page was built from the new code and the server was not restarted: what happened after the first update."""
    said = problems.invalid(refused(bogus=1), stale=True)
    assert said == ('"bogus" is not a setting this server knows. The server still runs the code from before the last '
                    "update: press Ctrl+C where HunterAI runs, then start it again (scripts/start.sh).")
    assert "start it again" not in problems.invalid(refused(risk={"read": "maybe"}), stale=True)      # a wrong value is not that


def test_several_things_wrong_are_all_said_up_to_three():
    assert problems.invalid(refused(a=1, b=2)) == '"a" is not a setting this server knows; "b" is not a setting this server knows.'
    assert problems.invalid(refused(a=1, b=2, c=3, d=4, e=5)).endswith('"c" is not a setting this server knows; and 2 more.')


def test_what_is_missing_is_said_as_missing():
    assert problems.invalid([{"type": "missing", "loc": ("body", "name"), "msg": "Field required"}]) == '"name" is missing.'
    assert problems.invalid([{"type": "model_type", "loc": ("body",), "msg": "Input should be a valid dictionary"}]) \
        == "input should be a valid dictionary."


async def test_permissions_the_server_refuses_say_which_one_and_why(client):
    r = await client.put("/api/settings", json={"permissions": {"risk": {"read": "maybe"}}})
    assert (r.status_code, r.json()["detail"]) == (400, "Permissions: \"risk.read\": input should be 'allow', 'ask' or 'deny'.")
    chat = (await client.post("/api/chats", json={"provider": "mock", "model": "mock-1"})).json()
    r = await client.patch(f"/api/chats/{chat['id']}", json={"permissions": {"bogus": True}})
    assert (r.status_code, r.json()["detail"]) == (400, 'Permissions: "bogus" is not a setting this server knows.')


async def test_permissions_that_are_no_object_are_said_as_that(client):
    r = await client.put("/api/settings", json={"permissions": ["allow"]})
    assert (r.status_code, r.json()["detail"]) == (422, '"permissions": input should be a valid dictionary.')


async def test_permissions_refused_by_a_server_on_old_code_say_to_restart(client, monkeypatch):
    monkeypatch.setattr(stale, "restart_needed", lambda: True)
    r = await client.put("/api/settings", json={"permissions": {"auto_everything": True}})
    assert r.status_code == 400 and r.json()["detail"].startswith('Permissions: "auto_everything" is not a setting this server knows. The server still runs')
    assert "scripts/start.sh" in r.json()["detail"]


async def test_a_request_with_a_wrong_value_says_which_field(client):
    r = await client.put("/api/settings", json={"max_turns": "many"})
    assert r.status_code == 422
    assert r.json()["detail"] == '"max_turns": input should be a valid integer, unable to parse string as an integer.'


# ---- a model call that failed

def failed(kind, message, provider="openai", model="gpt-5", **more) -> ProviderError:
    e = ProviderError(kind, message, **more)
    e.provider, e.model = provider, model
    return e


def test_a_key_that_is_not_there_says_where_to_enter_it():
    assert problems.model_call(failed("auth", "OPENAI_API_KEY is not set")) == (
        "OPENAI_API_KEY is not set. Enter the key under Settings, or pick a model of another provider for this chat.")


def test_a_key_the_provider_refuses_says_to_check_it():
    assert problems.model_call(failed("auth", "HTTP 401: Incorrect API key provided", status=401)) == (
        "openai: HTTP 401: Incorrect API key provided. The provider did not accept the key: check it under Settings.")


def test_no_credit_says_to_add_some_or_to_pick_another_model():
    assert problems.model_call(failed("credits", "HTTP 402: Insufficient balance", provider="abliteration")) == (
        "abliteration: HTTP 402: Insufficient balance. The account has no credit for this model: add credit at the provider, "
        "or pick another model for this chat.")


def test_a_rate_limit_says_to_wait_and_for_how_long_when_the_provider_said():
    assert problems.model_call(failed("rate_limit", "HTTP 429: slow down", retry_after=42.3)) == (
        "openai: HTTP 429: slow down. The provider limits how often this key may ask: wait 43 s, then send the message again.")
    assert problems.model_call(failed("rate_limit", "HTTP 429: slow down")).endswith("wait a minute, then send the message again.")


def test_no_answer_in_time_says_to_send_again():
    assert problems.model_call(failed("timeout", "abliteration timed out", provider="abliteration")) == (
        "abliteration timed out. No answer came in time: send the message again. If it keeps happening, the provider is slow or down.")


def test_a_provider_that_cannot_be_reached_names_the_connection_and_the_proxy():
    assert problems.model_call(failed("unavailable", "Anthropic API unreachable", provider="anthropic")) == (
        "Anthropic API unreachable. It could not be reached, or it failed on its side: check the internet connection (and the "
        "proxy for model providers under Settings, if you set one), then send the message again.")


def test_ollama_that_does_not_answer_says_to_start_it():
    said = problems.model_call(failed("unavailable", "Ollama not reachable at http://127.0.0.1:11434 (ConnectError)", provider="ollama"))
    assert said == ("Ollama not reachable at http://127.0.0.1:11434 (ConnectError). Start Ollama (`ollama serve`) and see with "
                    "`ollama ps` that the model fits this machine's memory, then send the message again.")


def test_a_program_that_is_not_installed_says_to_install_it():
    assert problems.model_call(failed("unavailable", "codex CLI is not installed or not on PATH", provider="codex")) == (
        "codex CLI is not installed or not on PATH. Install it, or pick another model for this chat.")


def test_a_refusal_says_to_reword_or_to_pick_another_model():
    assert problems.model_call(failed("policy", "The model declined this request (refusal).", provider="anthropic")) == (
        "anthropic: The model declined this request (refusal). The provider or the model refused it: reword the request, or "
        "pick another model for this chat.")


def test_a_request_the_provider_does_not_accept_points_at_the_model_and_the_options():
    assert problems.model_call(failed("bad_request", "HTTP 404: model gpt-9 does not exist", status=404)) == (
        "openai: HTTP 404: model gpt-9 does not exist. The provider did not accept the request as it was sent: if its words name "
        "the model, pick another one; if they name an option, change it under Options.")


def test_an_error_that_already_says_what_to_do_is_left_as_it_is():
    whole = TOO_LARGE.format(model="gemma:12b", need=25608, window=16384)
    assert problems.model_call(failed("bad_request", whole, provider="ollama", complete=True)) == whole
    no_such = 'This chat is set to the provider "gone", which this HunterAI does not have. Pick a model from the list for this chat.'
    assert problems.model_call(failed("bad_request", no_such, provider="gone", complete=True)) == no_such


def test_an_error_without_a_provider_is_still_said():
    assert problems.model_call(ProviderError("timeout", "it timed out")).startswith("it timed out. No answer came in time")


async def test_a_run_that_failed_says_what_to_do(client):
    chat = await new_chat(client)
    out = await send(client, chat["id"], "fail:credits")
    end = out["events"][-1]
    assert end["type"] == "run_finished" and end["status"] == "error"
    assert end["error"] == ("mock failure. The account has no credit for this model: add credit at the provider, or pick "
                            "another model for this chat.")


async def test_a_failed_run_names_the_provider_when_its_words_do_not(client, monkeypatch):
    from hunterai.providers.mock import MockProvider

    async def refused(self, *args, **kwargs):
        raise ProviderError("auth", "HTTP 401: bad key", 401)
        yield
    monkeypatch.setattr(MockProvider, "stream", refused)
    chat = await new_chat(client)
    end = (await send(client, chat["id"], "hi"))["events"][-1]
    assert end["error"] == "mock: HTTP 401: bad key. The provider did not accept the key: check it under Settings."


async def test_a_chat_set_to_a_provider_that_is_not_there_says_to_pick_a_model(conn):
    from hunterai import db
    from hunterai.repo import chats, messages, runs
    from hunterai.types import TextBlock

    async def no_bytes(upload_id):
        return b""
    chat = await chats.create(conn, None, "pentesting", "nonesuch", "model-x", {})
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="hi")])
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    run = await runs.get(conn, run_id)
    assert run.status == "error"
    assert run.error == ('This chat is set to the provider "nonesuch", which this HunterAI does not have. Pick a model from the '
                         "list for this chat.")


# ---- a fault of HunterAI itself

def boom():
    def inner():
        raise KeyError("active_leaf")
    inner()


def test_a_fault_of_hunterai_is_said_as_one_and_its_trace_is_kept(isolated_home):
    try:
        boom()
    except KeyError as e:
        said = problems.own_failure(e, "working on this message")
    log = config.logs_dir() / "errors.log"
    assert said == (f"HunterAI failed while working on this message (KeyError: 'active_leaf'). This is a fault in HunterAI, not in "
                    f"what you sent. Trying again may work. To report it: the full error is at the end of {log}, and \"Copy "
                    "diagnostics\" in the setup check gives the state of this install.")
    kept = log.read_text()
    assert "while working on this message" in kept and "Traceback (most recent call last)" in kept
    assert "in inner" in kept and "KeyError: 'active_leaf'" in kept


def test_the_file_of_faults_keeps_the_newest_and_does_not_grow_without_end(isolated_home, monkeypatch):
    monkeypatch.setattr(problems, "LOG_BYTES", 2000)
    for n in range(120):
        problems.own_failure(RuntimeError(f"fault number {n}"), "testing")
    log = config.logs_dir() / "errors.log"
    assert log.stat().st_size <= 2000 + 600 and "fault number 119" in log.read_text()
    before = log.with_name("errors.log.1")
    assert before.exists() and "fault number 0\n" not in log.read_text() + before.read_text()


def test_a_fault_is_said_even_when_its_trace_cannot_be_kept(isolated_home, monkeypatch):
    monkeypatch.setattr(config, "logs_dir", lambda: config.home() / "no" / "such" / "folder")
    said = problems.own_failure(ValueError("x" * 500), "testing")
    assert said.startswith("HunterAI failed while testing (ValueError: " + "x" * 200 + "…). This is a fault in HunterAI")
    assert "the full error is" not in said and "Copy diagnostics" in said


async def test_a_request_hunterai_fails_on_answers_in_words(isolated_home, monkeypatch):
    def broken():
        raise RuntimeError("the folder vanished")
    monkeypatch.setattr(system.toolbox, "installed", broken)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=BASE) as c:
            r = await c.get("/api/system/toolbox")
    assert r.status_code == 500
    assert r.json()["detail"].startswith("HunterAI failed while answering GET /api/system/toolbox (RuntimeError: the folder vanished). "
                                         "This is a fault in HunterAI, not in what you sent.")
    assert "the folder vanished" in (config.logs_dir() / "errors.log").read_text()


async def test_a_run_hunterai_fails_inside_says_so(client, monkeypatch):
    async def broken(*args, **kwargs):
        raise KeyError("active_leaf")
    monkeypatch.setattr(engine, "_context", broken)
    chat = await new_chat(client)
    end = (await send(client, chat["id"], "hi"))["events"][-1]
    assert end["status"] == "error"
    assert end["error"].startswith("HunterAI failed while working on this message (KeyError: 'active_leaf'). This is a fault in HunterAI")


async def test_a_fault_inside_the_call_to_a_model_names_the_model(client, monkeypatch):
    from hunterai.providers.mock import MockProvider

    async def broken(self, *args, **kwargs):
        raise AttributeError("'NoneType' object has no attribute 'parts'")
        yield
    monkeypatch.setattr(MockProvider, "stream", broken)
    chat = await new_chat(client)
    end = (await send(client, chat["id"], "hi"))["events"][-1]
    assert end["status"] == "error"
    assert end["error"].startswith("HunterAI failed while calling mock/mock-1 (AttributeError: 'NoneType' object has no attribute "
                                   "'parts'). This is a fault in HunterAI")
    assert end["error"].endswith("gives the state of this install.")           # and no advice about the provider after it


# ---- an answer that did not end by itself

@pytest.mark.parametrize("reason", ["end", "stop", "end_turn", "STOP", "tool_calls", "tool_use", "stop_sequence", None, ""])
def test_an_answer_that_ended_by_itself_needs_no_word(reason):
    assert problems.cut_off(reason, provider="openai") is None


def test_an_answer_cut_at_the_limit_the_chat_sets_says_where_to_raise_it():
    assert problems.cut_off("length", provider="abliteration", max_tokens=512, output_tokens=512) == ("cut_off", (
        'The answer was cut off at the 512 output tokens this chat allows. Send "continue" to have the model go on, or raise '
        '"Max output tokens" under Options.'))


def test_an_answer_cut_by_the_window_of_a_local_model_says_so():
    """The window holds the request and the answer together: with a request that nearly fills it, a few words fit."""
    assert problems.cut_off("length", provider="ollama", output_tokens=180, window=16384) == ("cut_off", (
        "The answer was cut off: the context window of the local model (16,384 tokens) was full. It holds the request and the "
        'answer together. Raise "Context window" under Options for this chat (a larger window needs more memory), or continue '
        "in a new chat."))
    assert "the context window of the local model was full" in problems.cut_off("length", provider="ollama")[1]


def test_the_word_for_a_full_local_window_names_the_chats_own_window_not_the_one_in_settings():
    """A chat whose window grew has its own from then on: the size in Settings no longer reaches it."""
    said = problems.cut_off("length", provider="ollama", output_tokens=180, window=32768)[1]
    assert '"Context window" under Options' in said and "Settings" not in said


def test_an_answer_that_repeats_itself_is_told_from_one_that_needed_more_room():
    said = problems.cut_off("length", provider="ollama", output_tokens=9000, window=16384, circles=True)
    assert said == ("cut_off", (
        "The answer was cut off: the context window of the local model (16,384 tokens) was full. Its end says the same thing "
        "over and over, so it was not written again in a larger window. Send the message again, or ask in another way."))
    # Only a local model's window grows: for any other, nothing was held back.
    assert problems.cut_off("length", provider="openai", output_tokens=9000, circles=True) == \
        problems.cut_off("length", provider="openai", output_tokens=9000)


def test_a_local_answer_cut_at_the_limit_the_chat_sets_is_told_from_a_full_window():
    assert "512 output tokens this chat allows" in problems.cut_off("length", provider="ollama", max_tokens=512, output_tokens=512,
                                                                     window=16384)[1]
    assert "context window of the local model" in problems.cut_off("length", provider="ollama", max_tokens=4096, output_tokens=200,
                                                                    window=16384)[1]


def test_a_window_that_was_raised_says_from_what_to_what_why_and_whose_it_is():
    assert problems.window_grew(16384, 65536) == (
        "The context window of this chat was raised from 16,384 to 65,536 tokens: the chat did not fit the smaller one. It is "
        'this chat\'s own now (Context window under Options). A window grows by itself up to "Largest context window for local '
        'models" in Settings (a larger window needs more memory; 0 there keeps a window as it is set).')
    again = problems.window_grew(16384, 32768, again=True)
    assert "the answer filled the smaller one and was written again" in again and "did not fit" not in again
    lent = problems.window_grew(16384, 65536, kept=False)
    assert "It holds for this answer: the chat's own model was not the one that answered." in lent and "own now" not in lent


def test_a_window_that_could_not_be_raised_says_what_the_model_said_in_a_line():
    said = problems.window_stayed(16384, 65536, "HTTP 500: llama runner process has terminated:\n  exit status 2\n")
    assert said == ("The context window of this chat stays at 16,384 tokens: the local model did not answer with one of 65,536 "
                    "(HTTP 500: llama runner process has terminated: exit status 2). HunterAI does not try that size again for "
                    "this model until it is started again; what does not fit the window is shortened as before.")
    long = problems.window_stayed(16384, 65536, "x" * 500)
    assert "(" + "x" * 200 + "…)" in long and "x" * 201 not in long


@pytest.mark.parametrize("reason,provider", [("max_tokens", "anthropic"), ("MAX_TOKENS", "google"), ("length", "openai")])
def test_an_answer_cut_at_the_limit_of_the_model_says_how_to_go_on(reason, provider):
    assert problems.cut_off(reason, provider=provider, output_tokens=8192) == ("cut_off", (
        'The answer was cut off at the limit the model has for one answer. Send "continue" to have it go on. If the model '
        'takes "Max output tokens" under Options, a higher value there gives it more room.'))


def test_a_full_window_of_a_model_that_is_not_local_says_to_continue_elsewhere():
    assert problems.cut_off("model_context_window_exceeded", provider="anthropic") == ("cut_off", (
        "The answer was cut off: the context window of the model was full. Continue in a new chat, or choose a model with a "
        "larger context window."))


@pytest.mark.parametrize("reason", ["content_filter", "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"])
def test_an_answer_the_filter_of_the_provider_ended_says_so(reason):
    assert problems.cut_off(reason, provider="google") == ("filtered", (
        f"The answer stops here because the content filter of the provider ended it ({reason}). Reword the request, or pick "
        "another model for this chat."))


def cut_at(reason, text="The first step is to", usage=(900, 40)):
    from hunterai.providers.base import Stop, TextDelta, Usage
    seen = []

    async def stream(self, model, system, messages, options, tools=None):
        seen.append(messages)
        yield TextDelta(text)
        yield Usage(*usage)
        yield Stop(reason)
    return stream, seen


async def test_a_cut_answer_carries_the_word_about_it_under_it(client, monkeypatch):
    from hunterai.providers.mock import MockProvider
    stream, _ = cut_at("length")
    monkeypatch.setattr(MockProvider, "stream", stream)
    chat = await new_chat(client)
    end = (await send(client, chat["id"], "explain the whole flow"))["events"][-1]
    assert end["status"] == "done" and end["error"] is None
    blocks = end["message"]["blocks"]
    assert [b["type"] for b in blocks] == ["text", "notice"]
    assert blocks[1]["kind"] == "cut_off" and blocks[1]["text"].startswith("The answer was cut off at the limit the model has")
    stored = (await client.get(f"/api/chats/{chat['id']}")).json()["messages"][-1]["blocks"]
    assert stored == blocks                                            # it is still there after a reload


async def test_the_word_about_a_cut_answer_is_the_owners_and_never_goes_to_a_model(client, monkeypatch):
    from hunterai.providers.mock import MockProvider
    stream, seen = cut_at("length")
    monkeypatch.setattr(MockProvider, "stream", stream)
    chat = await new_chat(client)
    await send(client, chat["id"], "explain the whole flow")
    await send(client, chat["id"], "continue")
    sent = [b.type for m in seen[-1] for b in m.blocks]
    assert sent == ["text", "text", "text"] and all("cut off" not in getattr(b, "text", "") for m in seen[-1] for b in m.blocks)


async def test_an_answer_that_ended_by_itself_has_no_word_under_it(client):
    chat = await new_chat(client)
    end = (await send(client, chat["id"], "hi"))["events"][-1]
    assert "notice" not in [b["type"] for b in end["message"]["blocks"]]


async def test_a_cut_answer_at_the_limit_of_the_chat_names_that_limit(client, monkeypatch):
    from hunterai.providers.mock import MockProvider
    stream, _ = cut_at("length", usage=(900, 256))
    monkeypatch.setattr(MockProvider, "stream", stream)
    chat = await new_chat(client, options={"max_tokens": 256})
    end = (await send(client, chat["id"], "explain the whole flow"))["events"][-1]
    assert "256 output tokens this chat allows" in end["message"]["blocks"][-1]["text"]


# ---- a page that is older than the server

PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>HunterAI</title>
    <script type="module" crossorigin src="/assets/index-CXRQE8sX.js"></script>
    <link rel="stylesheet" crossorigin href="/assets/index-B1dQmA.css">
  </head>
  <body><div id="root"></div></body>
</html>
"""


async def page(client):
    return (await client.get("/api/system/status")).json()["page"]


async def test_the_status_names_the_script_of_the_interface_that_is_built_now(client, monkeypatch, tmp_path):
    """A page that was opened before an update runs another script than this one: it can tell, and say to reload."""
    monkeypatch.setattr(config, "dist_dir", lambda: tmp_path)
    assert await page(client) is None                                  # not built
    (tmp_path / "index.html").write_text(PAGE)
    assert await page(client) == "/assets/index-CXRQE8sX.js"
    (tmp_path / "index.html").write_text(PAGE.replace("index-CXRQE8sX.js", "index-Zq81LmP0w.js"))      # built again
    assert await page(client) == "/assets/index-Zq81LmP0w.js"


async def test_a_page_file_without_a_script_of_its_own_names_none(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "dist_dir", lambda: tmp_path)
    (tmp_path / "index.html").write_text("<html><script src='https://elsewhere.example/x.js'></script></html>")
    assert await page(client) is None
