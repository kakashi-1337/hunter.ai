"""The context window of a local model grows by itself. A chat that does not fit the window it has gets a larger
one before anything of it is left out, up to the largest the owner allows, and an answer that filled its window is
written again in a larger one. Until now the owner did that by hand: raise "Context window" under Options, send
again. Ollama is the real provider here, against a stand-in server."""
import json

import httpx
import respx

from hunterai import context, cooling, db, engine, hub, thermal
from hunterai.providers import ollama, registry
from hunterai.providers.base import ModelInfo, Options, ProviderError
from hunterai.repo import chats, messages, runs
from hunterai.tools import registry as tools
from hunterai.types import TextBlock, ThinkingBlock
from tests.toolkit import fake_tool

HOST = "http://127.0.0.1:11434"
MODEL = "qwen3:8b"
ANSWER = "Next I would try the account list with another user's id."
LATEST = "What should we try next on that endpoint?"


def line(obj):
    return json.dumps(obj) + "\n"


def ending(ended, produced):
    return line({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": ended,
                 "prompt_eval_count": 900, "eval_count": produced})


def written(text=ANSWER, ended="stop", produced=12):
    """What the stand-in Ollama sends for one answer. `ended`: why it says it stopped writing, after `produced` tokens."""
    return httpx.Response(200, text=line({"message": {"role": "assistant", "content": text}, "done": False}) + ending(ended, produced))


def calls_tool(name, ended="stop", **arguments):
    """What the stand-in Ollama sends for a turn in which the model calls a tool."""
    return httpx.Response(200, text=line({"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": arguments}}]}, "done": False}) + ending(ended, 12))


def answers(*replies):
    """The stand-in Ollama answers its calls with these, one per call and the last one from then on (none: ANSWER).
    Returns the route of /api/chat, which holds every request it got."""
    left = list(replies) or [written()]
    return respx.post(HOST + "/api/chat").mock(side_effect=lambda request: left.pop(0) if len(left) > 1 else left[0])


def bodies(route):
    return [json.loads(c.request.content) for c in route.calls]


def said(body) -> str:
    return "\n".join(m.get("content") or "" for m in body["messages"] if m["role"] != "system")


async def long_chat(conn, turns=14, size=3000, provider="ollama", model=MODEL, options=None):
    """A chat that has gone on for a while: `turns` questions and answers of `size` characters each, then one more
    question. Returns (chat, the last user message)."""
    chat = await chats.create(conn, None, "pentesting", provider, model, options or {})
    parent = None
    for n in range(1, turns + 1):
        user = await messages.add(conn, chat.id, parent, "user", [TextBlock(text=f"question {n}: " + "words " * (size // 6))])
        reply = await messages.add(conn, chat.id, user.id, "assistant", [TextBlock(text=f"answer {n}: " + "found " * (size // 6))],
                                   provider=provider, model=model)
        parent = reply.id
    return chat, await messages.add(conn, chat.id, parent, "user", [TextBlock(text=LATEST)])


async def no_bytes(upload_id):
    return b""


async def go(conn, chat, user):
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    return await runs.get(conn, run_id), await messages.get(conn, mid)


async def events(run, *kinds):
    """What the run told the page while it went on, in order: its events of these kinds."""
    return [e async for e in hub.subscribe(run.id) if e["type"] in kinds]


def kinds(run, *wanted):
    """The steps of these kinds that the run took, in order: (kind, name)."""
    return [(s.kind, s.name) for s in run.steps if s.kind in wanted]


def shown(context_length=None):
    """What the stand-in Ollama says of a model: it takes `context_length` tokens at most (None: its file does not say)."""
    info = {"general.architecture": "qwen3"}
    if context_length:
        info["qwen3.context_length"] = context_length
    return respx.post(HOST + "/api/show").mock(return_value=httpx.Response(200, json={
        "capabilities": ["completion", "tools"], "model_info": info}))


def test_a_window_may_grow_to_131072_unless_the_owner_says_otherwise(monkeypatch):
    monkeypatch.delenv(ollama.MOST_CTX_ENV, raising=False)
    assert ollama.largest_num_ctx() == 131_072
    monkeypatch.setenv(ollama.MOST_CTX_ENV, "65536")
    assert ollama.largest_num_ctx() == 65_536
    monkeypatch.setenv(ollama.MOST_CTX_ENV, "0")
    assert ollama.largest_num_ctx() == 0            # the owner switched growing off
    monkeypatch.setenv(ollama.MOST_CTX_ENV, "a lot")
    assert ollama.largest_num_ctx() == 131_072      # what names no size is no switch


@respx.mock
async def test_the_larger_windows_of_a_call_double_up_to_the_largest_allowed():
    shown()
    p = registry.get("ollama")
    assert await p.larger_windows(MODEL, Options()) == [32_768, 65_536, 131_072]
    assert await p.larger_windows(MODEL, Options(context_window=20_000)) == [40_000, 80_000, 131_072]
    assert await p.larger_windows(MODEL, Options(context_window=100_000)) == [131_072]


@respx.mock
async def test_a_window_never_grows_past_what_the_model_takes():
    shown(40_960)
    p = registry.get("ollama")
    assert await p.larger_windows(MODEL, Options()) == [32_768, 40_960]
    assert await p.larger_windows(MODEL, Options(context_window=40_960)) == []


@respx.mock
async def test_a_window_at_the_largest_or_over_it_stays_as_it_is(monkeypatch):
    shown()
    p = registry.get("ollama")
    assert await p.larger_windows(MODEL, Options(context_window=131_072)) == []
    assert await p.larger_windows(MODEL, Options(context_window=262_144)) == []     # the owner's own choice is not made smaller
    monkeypatch.setenv(ollama.MOST_CTX_ENV, "0")
    assert await p.larger_windows(MODEL, Options()) == []


@respx.mock
async def test_a_chat_that_does_not_fit_its_window_gets_a_larger_one_and_is_sent_whole(conn):
    shown()
    route = answers()
    chat, user = await long_chat(conn)                  # about 84,000 characters: more than a window of 32,768 holds
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    sent = bodies(route)
    assert len(sent) == 1                               # nothing was summarized for it
    assert sent[0]["options"]["num_ctx"] == 65_536      # doubled twice: the first window that holds the chat
    assert "question 1:" in said(sent[0]) and context.SUMMARY_MARK not in said(sent[0])
    assert said(sent[0]).rstrip().endswith(LATEST)
    assert (await chats.get(conn, chat.id)).options == {"context_window": 65_536}       # the chat's own from now on
    assert [(s.kind, s.name, s.status) for s in run.steps] == [
        ("context", "context window 16,384 → 65,536", "done"), ("model_call", f"ollama/{MODEL}", "done")]
    assert run.steps[0].detail == {"window": {"from": 16_384, "to": 65_536, "why": "request"}}
    assert [b.type for b in msg.blocks] == ["text", "notice"]
    assert msg.blocks[0].text == ANSWER and msg.blocks[1].kind == "window"
    assert msg.blocks[1].text.startswith("The context window of this chat was raised from 16,384 to 65,536 tokens: the chat "
                                         "did not fit the smaller one.")
    assert "this chat's own" in msg.blocks[1].text and "Settings" in msg.blocks[1].text


@respx.mock
async def test_the_other_options_of_a_chat_stay_as_they_are_when_its_window_is_raised(conn):
    shown()
    answers()
    chat, user = await long_chat(conn, options={"temperature": 0.2, "thinking": False})
    await go(conn, chat, user)
    assert (await chats.get(conn, chat.id)).options == {"temperature": 0.2, "thinking": False, "context_window": 65_536}


SUMMARY = "The user is mapping the login of shop.example. /admin answered 403; /api/v2/users lists accounts without a session."


def is_summary(body) -> bool:
    return body["messages"][0]["content"] == context.SUMMARY_SYSTEM


@respx.mock
async def test_past_the_largest_window_a_chat_is_shortened_as_before_and_its_summary_is_asked_for_in_that_window(conn):
    shown()
    route = answers(written(SUMMARY), written())
    chat, user = await long_chat(conn, turns=60, size=9000)      # over a million characters: no window up to 131,072 holds it
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    asked, sent = bodies(route)
    assert is_summary(asked) and not is_summary(sent)
    # The summary is a call to the same model. With another window than the chat's, Ollama loads the model again for
    # it, and again for the answer; and what it is asked to summarize was measured for the chat's window.
    assert asked["options"]["num_ctx"] == sent["options"]["num_ctx"] == 131_072
    assert context.SUMMARY_MARK in said(sent) and SUMMARY in said(sent)
    assert "question 1:" not in said(sent) and said(sent).rstrip().endswith(LATEST)
    assert ollama._chars(sent) / ollama.CHARS_PER_TOKEN <= 131_072
    assert (await chats.get(conn, chat.id)).options == {"context_window": 131_072}
    assert [(s.kind, s.name, s.status) for s in run.steps] == [
        ("context", "context window 16,384 → 131,072", "done"), ("model_call", f"ollama/{MODEL}", "done"),
        ("context", "summary of the earlier messages", "done")]
    assert msg.blocks[0].text == ANSWER and msg.blocks[1].kind == "window"


@respx.mock
async def test_with_growing_switched_off_the_window_stays_and_the_chat_is_shortened(conn, monkeypatch):
    monkeypatch.setenv(ollama.MOST_CTX_ENV, "0")
    shown()
    route = answers(written(SUMMARY), written())
    chat, user = await long_chat(conn)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    asked, sent = bodies(route)
    assert is_summary(asked) and asked["options"]["num_ctx"] == sent["options"]["num_ctx"] == 16_384
    assert context.SUMMARY_MARK in said(sent) and "question 1:" not in said(sent)
    assert (await chats.get(conn, chat.id)).options == {}
    assert [(s.kind, s.name) for s in run.steps] == [("model_call", f"ollama/{MODEL}"), ("context", "summary of the earlier messages")]
    assert [b.type for b in msg.blocks] == ["text"]


class Down:
    """A chat's own model, at a provider that cannot be reached: the local model answers in its place."""
    name = "abliteration"
    supports_tools = True

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="big", label="big", context_window=262144)]

    async def stream(self, model, system, msgs, options, tools=None):
        raise ProviderError("unavailable", "the provider cannot be reached", 503)
        yield       # a generator, like every provider's stream


@respx.mock
async def test_a_local_model_that_answers_in_place_of_the_chats_model_gets_the_larger_window_for_that_answer_only(conn):
    registry.override("abliteration", Down())
    shown()
    route = answers()
    chat, user = await long_chat(conn, provider="abliteration", model="big")
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert (msg.provider, msg.fallback_from) == ("ollama", "abliteration/big")
    sent = bodies(route)
    assert len(sent) == 1 and sent[0]["options"]["num_ctx"] == 65_536 and "question 1:" in said(sent[0])
    # The size is the local model's. Written into the chat it would be the window of the chat's own model, which has 262,144.
    assert (await chats.get(conn, chat.id)).options == {}
    assert [(s.kind, s.name) for s in run.steps] == [
        ("model_call", "abliteration/big"), ("fallback", "abliteration → ollama"),
        ("context", "context window 16,384 → 65,536"), ("model_call", f"ollama/{MODEL}")]
    assert msg.blocks[1].kind == "window"
    assert "It holds for this answer: the chat's own model was not the one that answered." in msg.blocks[1].text
    assert "this chat's own now" not in msg.blocks[1].text


CUT = "The first thing I would look at is whether the"       # an answer that stops in the middle of its sentence


@respx.mock
async def test_an_answer_that_filled_its_window_is_written_again_in_a_larger_one(conn):
    shown()
    route = answers(written(CUT, ended="length"), written())
    chat, user = await long_chat(conn, turns=0)             # one question: the request fits the window, the answer does not
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    first, second = bodies(route)
    assert (first["options"]["num_ctx"], second["options"]["num_ctx"]) == (16_384, 32_768)
    assert second["messages"] == first["messages"]          # asked the same again: the half answer is no part of the chat
    assert [b.type for b in msg.blocks] == ["text", "notice"]
    assert msg.blocks[0].text == ANSWER                     # the answer that ended by itself, and nothing of the other
    assert msg.blocks[1].kind == "window"
    assert msg.blocks[1].text.startswith("The context window of this chat was raised from 16,384 to 32,768 tokens: the answer "
                                         "filled the smaller one and was written again.")
    assert (await chats.get(conn, chat.id)).options == {"context_window": 32_768}
    assert [(s.kind, s.name, s.status) for s in run.steps] == [
        ("model_call", f"ollama/{MODEL}", "done"), ("context", "context window 16,384 → 32,768", "done"),
        ("model_call", f"ollama/{MODEL}", "done")]
    assert run.steps[0].detail["stop_reason"] == "length"
    assert run.steps[1].detail == {"window": {"from": 16_384, "to": 32_768, "why": "answer"}}
    # The page had the half answer on it: it is told to take it off before the new one begins.
    assert await events(run, "text_delta", "answer_restarted") == [
        {"type": "text_delta", "text": CUT}, {"type": "answer_restarted", "blocks": []}, {"type": "text_delta", "text": ANSWER}]


@respx.mock
async def test_an_answer_cut_at_the_output_tokens_the_chat_allows_is_not_written_again(conn):
    shown()
    route = answers(written(CUT, ended="length", produced=100), written())
    chat, user = await long_chat(conn, turns=0, options={"max_tokens": 100})
    run, msg = await go(conn, chat, user)
    assert (run.status, len(bodies(route))) == ("done", 1)          # a larger window gives that answer no more room
    assert [b.type for b in msg.blocks] == ["text", "notice"]
    assert msg.blocks[1].kind == "cut_off" and "100 output tokens this chat allows" in msg.blocks[1].text
    assert (await chats.get(conn, chat.id)).options == {"max_tokens": 100}
    assert [s.kind for s in run.steps] == ["model_call"]


@respx.mock
async def test_a_turn_that_called_a_tool_is_not_written_again_though_it_ended_at_the_limit(conn):
    tool, ran = fake_tool("t_look", text="uid=1000")
    tools.register(tool)
    shown()
    route = answers(calls_tool("t_look", ended="length", q=1), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    _, second = bodies(route)
    assert ran == [{"q": 1}]                                        # the call the model made is run, and run once
    assert second["messages"][-1] == {"role": "tool", "tool_name": "t_look", "content": "uid=1000"}
    assert second["options"]["num_ctx"] == 16_384
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_result", "text"]
    assert (await chats.get(conn, chat.id)).options == {}


@respx.mock
async def test_an_answer_that_fills_the_larger_window_too_is_left_as_it_is_and_says_so(conn):
    shown()
    route = answers(written(CUT, ended="length"), written(CUT + " session", ended="length"), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, len(bodies(route))) == ("done", 2)          # written again once, not for as long as it takes
    assert [b.type for b in msg.blocks] == ["text", "notice", "notice"]
    assert msg.blocks[0].text == CUT + " session"
    assert (msg.blocks[1].kind, msg.blocks[2].kind) == ("window", "cut_off")
    assert "the context window of the local model (32,768 tokens) was full" in msg.blocks[2].text
    assert (await chats.get(conn, chat.id)).options == {"context_window": 32_768}


@respx.mock
async def test_an_answer_that_fills_a_window_that_was_just_raised_is_written_again_in_the_next_one(conn):
    shown()
    route = answers(written(CUT, ended="length"), written())
    chat, user = await long_chat(conn)                              # the chat itself takes the window to 65,536
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert [b["options"]["num_ctx"] for b in bodies(route)] == [65_536, 131_072]
    assert kinds(run, "context") == [("context", "context window 16,384 → 65,536"), ("context", "context window 65,536 → 131,072")]
    assert (await chats.get(conn, chat.id)).options == {"context_window": 131_072}
    assert msg.blocks[0].text == ANSWER and [b.type for b in msg.blocks] == ["text", "notice"]
    assert msg.blocks[1].text.startswith("The context window of this chat was raised from 16,384 to 131,072 tokens: the answer "
                                         "filled the smaller one and was written again.")


CIRCLE = "I should check the login form again before I go on. "


@respx.mock
async def test_an_answer_that_goes_round_in_a_circle_is_not_written_again(conn):
    """A model that repeats itself fills any window. In a larger one it does the same for twice as long, on a machine
    that is at full power all the while."""
    shown()
    route = answers(written("Let me think. " + CIRCLE * 40, ended="length"), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, len(bodies(route))) == ("done", 1)
    assert [b.type for b in msg.blocks] == ["text", "notice"]
    assert msg.blocks[1].kind == "cut_off"
    assert "Its end says the same thing over and over, so it was not written again in a larger window." in msg.blocks[1].text
    assert (await chats.get(conn, chat.id)).options == {}
    assert [s.kind for s in run.steps] == ["model_call"]


def test_what_counts_as_going_round_in_a_circle():
    said = ("The token is checked on the server, the cookie is set for the parent domain, the redirect keeps the query, and "
            "the session is the same one after the login as before it. ")
    assert len(said) > engine.CIRCLE_TAIL
    assert engine._circles([TextBlock(text="First. " + said * 3)])                       # a paragraph, three times
    assert engine._circles([ThinkingBlock(text="plan"), TextBlock(text="no " * 200)])     # a word, over and over
    assert engine._circles([ThinkingBlock(text="Wait. " + CIRCLE * 12)])                  # the thinking counts as the answer does
    assert not engine._circles([TextBlock(text="First. " + said * 2)])                   # said twice is not a circle
    assert not engine._circles([TextBlock(text=CUT)])                                    # too short to tell
    assert not engine._circles([TextBlock(text=" ".join(f"finding {n} is in /api/v{n}/users" for n in range(200)))])
    assert not engine._circles([])
    assert not engine._circles([TextBlock(text="no no no no no no")])                    # shorter than what is looked for


NO_MEMORY = "model requires more system memory (41.2 GiB) than is available (27.9 GiB)"


def refuses(status=500, error=NO_MEMORY):
    """What the stand-in Ollama sends when it cannot load the model with the window it was asked for."""
    return httpx.Response(status, json={"error": error})


@respx.mock
async def test_a_window_the_machine_cannot_load_is_taken_back_and_the_chat_goes_on_as_before(conn):
    shown()
    route = answers(refuses(), written(SUMMARY), written())
    chat, user = await long_chat(conn)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    large, asked, sent = bodies(route)
    assert large["options"]["num_ctx"] == 65_536 and "question 1:" in said(large)
    assert is_summary(asked) and asked["options"]["num_ctx"] == sent["options"]["num_ctx"] == 16_384
    assert context.SUMMARY_MARK in said(sent) and said(sent).rstrip().endswith(LATEST)
    assert (await chats.get(conn, chat.id)).options == {}           # nothing of the size that did not work is left in the chat
    assert [(s.kind, s.name, s.status) for s in run.steps] == [
        ("context", "context window 16,384 → 65,536", "done"), ("model_call", f"ollama/{MODEL}", "error"),
        ("context", "context window 65,536 → 16,384", "done"), ("model_call", f"ollama/{MODEL}", "done"),
        ("context", "summary of the earlier messages", "done")]
    assert run.steps[2].detail == {"window": {"from": 65_536, "to": 16_384, "why": "failed", "error": "HTTP 500: " + NO_MEMORY}}
    assert [b.type for b in msg.blocks] == ["text", "notice"]
    assert (msg.blocks[0].text, msg.blocks[1].kind) == (ANSWER, "window")
    assert msg.blocks[1].text == (
        "The context window of this chat stays at 16,384 tokens: the local model did not answer with one of 65,536 "
        f"(HTTP 500: {NO_MEMORY}). HunterAI does not try that size again for this model until it is started again; what does "
        "not fit the window is shortened as before.")
    # Loading a model that does not fit takes its time, and the machine's: the size is not tried a second time.
    assert await registry.get("ollama").larger_windows(MODEL, Options()) == [32_768]


@respx.mock
async def test_a_chat_that_had_a_window_of_its_own_has_it_again_when_the_larger_one_cannot_be_loaded(conn):
    shown()
    route = answers(refuses(), written(SUMMARY), written())
    chat, user = await long_chat(conn, options={"context_window": 20_000, "temperature": 0.2})
    run, _ = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert [b["options"]["num_ctx"] for b in bodies(route)] == [80_000, 20_000, 20_000]     # 40,000 would not hold the chat
    assert (await chats.get(conn, chat.id)).options == {"context_window": 20_000, "temperature": 0.2}
    assert kinds(run, "context") == [("context", "context window 20,000 → 80,000"), ("context", "context window 80,000 → 20,000"),
                                     ("context", "summary of the earlier messages")]


@respx.mock
async def test_a_window_the_owner_set_while_the_larger_one_was_tried_is_not_taken_from_the_chat(conn):
    """A model that does not fit can take a minute to say so. The owner does not wait: he sets a size under Options."""
    shown()
    chat, user = await long_chat(conn)

    async def reply(request):
        if json.loads(request.content)["options"]["num_ctx"] == 65_536:
            await chats.update(conn, chat.id, options={"context_window": 24_576, "temperature": 0.2})
            return refuses()
        return written(SUMMARY) if is_summary(json.loads(request.content)) else written()
    respx.post(HOST + "/api/chat").mock(side_effect=reply)
    run, _ = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert (await chats.get(conn, chat.id)).options == {"context_window": 24_576, "temperature": 0.2}


@respx.mock
async def test_when_the_larger_window_for_an_answer_cannot_be_loaded_the_answer_that_was_cut_off_stays(conn):
    shown()
    route = answers(written(CUT, ended="length"), refuses())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert [b["options"]["num_ctx"] for b in bodies(route)] == [16_384, 32_768]
    assert [b.type for b in msg.blocks] == ["text", "notice", "notice"]
    assert msg.blocks[0].text == CUT                                # what the model had written is back, with what it lacks said
    assert (msg.blocks[1].kind, msg.blocks[2].kind) == ("window", "cut_off")
    assert msg.blocks[1].text.startswith("The context window of this chat stays at 16,384 tokens: the local model did not "
                                         "answer with one of 32,768 (HTTP 500: ")
    assert "the context window of the local model (16,384 tokens) was full" in msg.blocks[2].text
    assert (await chats.get(conn, chat.id)).options == {}
    assert [(s.kind, s.name, s.status) for s in run.steps] == [
        ("model_call", f"ollama/{MODEL}", "done"), ("context", "context window 16,384 → 32,768", "done"),
        ("model_call", f"ollama/{MODEL}", "error"), ("context", "context window 32,768 → 16,384", "done")]
    half = {"type": "text", "text": CUT}
    assert await events(run, "answer_restarted") == [
        {"type": "answer_restarted", "blocks": []}, {"type": "answer_restarted", "blocks": [half]}]
    assert await registry.get("ollama").larger_windows(MODEL, Options()) == []


@respx.mock
async def test_an_ollama_that_is_not_there_is_no_reason_to_give_up_a_size(conn):
    """The call with the larger window fails, and so does the call with the smaller one: the size was not the reason."""
    shown()
    route = respx.post(HOST + "/api/chat").mock(side_effect=httpx.ConnectError("connection refused"))
    chat, user = await long_chat(conn)
    run, msg = await go(conn, chat, user)
    assert run.status == "error" and "Ollama not reachable" in run.error
    assert [b["options"]["num_ctx"] for b in bodies(route)][:2] == [65_536, 16_384]
    assert (await chats.get(conn, chat.id)).options == {}
    assert msg.blocks == []                                         # no word about a window: the run's error says what happened
    assert await registry.get("ollama").larger_windows(MODEL, Options()) == [32_768, 65_536, 131_072]


def machine(monkeypatch, *degrees, in_memory=True):
    """The processor says these, one per look and the last one from then on; Ollama says whether the model is in
    memory. Returns what the processor has left to say."""
    for name in ("HUNTERAI_MAX_CPU_TEMP", "HUNTERAI_MAX_GPU_TEMP"):
        monkeypatch.delenv(name, raising=False)
    left = list(degrees)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: left.pop(0) if len(left) > 1 else left[0])

    async def held(model):
        return in_memory
    monkeypatch.setattr(ollama, "in_memory", held)
    return left


@respx.mock
async def test_a_hot_machine_cools_before_its_model_is_loaded_again_for_a_larger_window(conn, monkeypatch):
    """A window of another size is another load of the model: several GB read with the processor at full power, on a
    machine that has just been answering. That the model is in memory does not make this call a light one."""
    monkeypatch.setattr(cooling, "STEP", 0)
    left = machine(monkeypatch, 93.0, 88.0, 60.0)
    shown()
    route = answers()
    chat, user = await long_chat(conn)
    run, _ = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert [(s.kind, s.status) for s in run.steps] == [("cooling", "done"), ("context", "done"), ("model_call", "done")]
    assert left == [60.0]                                           # it waited until the processor was five degrees under its limit
    assert bodies(route)[0]["options"]["num_ctx"] == 65_536


@respx.mock
async def test_a_machine_that_does_not_cool_keeps_its_window_and_the_run_says_why_it_stopped(conn, monkeypatch):
    monkeypatch.setattr(cooling, "WAIT", 0.02)
    monkeypatch.setattr(cooling, "STEP", 0.001)
    machine(monkeypatch, 97.0)
    shown()
    route = answers()
    chat, user = await long_chat(conn)
    run, _ = await go(conn, chat, user)
    assert run.status == "error" and bodies(route) == []
    assert run.error.startswith("Stopped: the processor is at 97 °C and did not cool to 85 °C")
    assert "has to be loaded again for a larger context window" in run.error and "not in memory" not in run.error
    assert (await chats.get(conn, chat.id)).options == {}
    assert [s.kind for s in run.steps] == ["cooling"]


@respx.mock
async def test_an_answer_that_was_cut_off_stays_when_the_machine_is_too_hot_to_write_it_again(conn, monkeypatch):
    monkeypatch.setattr(cooling, "WAIT", 0.02)
    monkeypatch.setattr(cooling, "STEP", 0.001)
    machine(monkeypatch, 97.0)
    shown()
    route = answers(written(CUT, ended="length"), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert run.status == "error" and len(bodies(route)) == 1
    assert "has to be loaded again for a larger context window" in run.error
    assert [b.type for b in msg.blocks] == ["text"] and msg.blocks[0].text == CUT
    assert await events(run, "answer_restarted") == []
    assert (await chats.get(conn, chat.id)).options == {}


PAGE = "<tr><td>order</td><td>shipped</td></tr>\n" * 750             # what a tool gave: about 30,000 characters


@respx.mock
async def test_a_run_whose_tool_result_does_not_fit_gets_a_larger_window_before_its_next_call(conn):
    """What the owner did by hand until now: a tool gave more than the window holds beside the rules, the run ended
    with "The request does not fit the context window", he raised the window under Options and sent the message again."""
    fetch, fetched = fake_tool("t_fetch", text=PAGE)
    look, _ = fake_tool("t_look", text="uid=1000")
    tools.register(fetch)
    tools.register(look)
    shown()
    route = answers(calls_tool("t_fetch", url="https://shop.example/orders"), calls_tool("t_look", q=1), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    first, second, third = bodies(route)
    assert [b["options"]["num_ctx"] for b in (first, second, third)] == [16_384, 32_768, 32_768]
    for sent in (second, third):                                    # the model reads all of what the tool gave
        assert PAGE in said(sent) and context.STUB_MARK not in said(sent) and context.SUMMARY_MARK not in said(sent)
    assert len(fetched) == 1                                        # nothing was run a second time for it
    # Raised once, where the run needed it. The calls after that one are made with the window it has from then on.
    assert kinds(run, "context", "model_call") == [
        ("model_call", f"ollama/{MODEL}"), ("context", "context window 16,384 → 32,768"),
        ("model_call", f"ollama/{MODEL}"), ("model_call", f"ollama/{MODEL}")]
    assert (await chats.get(conn, chat.id)).options == {"context_window": 32_768}
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_result", "tool_call", "tool_result", "text", "notice"]
    assert msg.blocks[-1].kind == "window"


def calls_twice(name, **arguments):
    """A turn of the stand-in Ollama in which the model calls one tool two times."""
    call = {"function": {"name": name, "arguments": arguments}}
    return httpx.Response(200, text=line({"message": {"role": "assistant", "content": "", "tool_calls": [call, call]}, "done": False})
                          + ending("stop", 12))


@respx.mock
async def test_a_window_raised_twice_in_one_run_is_said_once_from_where_it_began_to_where_it_ended(conn):
    fetch, _ = fake_tool("t_fetch", text=PAGE)
    tools.register(fetch)
    shown()
    route = answers(calls_twice("t_fetch", url="https://shop.example/orders"), written())
    chat, user = await long_chat(conn)                              # the chat needs 65,536; with two pages more, 131,072
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert [b["options"]["num_ctx"] for b in bodies(route)] == [65_536, 131_072]
    assert kinds(run, "context") == [("context", "context window 16,384 → 65,536"), ("context", "context window 65,536 → 131,072")]
    assert (await chats.get(conn, chat.id)).options == {"context_window": 131_072}
    said_under = [b.text for b in msg.blocks if b.type == "notice"]
    assert len(said_under) == 1
    assert said_under[0].startswith("The context window of this chat was raised from 16,384 to 131,072 tokens: the chat did "
                                    "not fit the smaller one.")


@respx.mock
async def test_what_fits_no_window_is_refused_with_the_largest_one_named_and_not_tried_in_a_smaller(conn):
    shown()
    route = answers()
    chat = await chats.create(conn, None, "pentesting", "ollama", MODEL, {})
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="Read this dump: " + "0x41 " * 100_000)])
    run, _ = await go(conn, chat, user)                             # one message of 500,000 characters: nothing to shorten
    assert run.status == "error" and bodies(route) == []
    assert "does not fit the context window of the local model" in run.error
    assert "for a window of 131,072" in run.error and "16,384" not in run.error
    assert kinds(run, "context", "model_call") == [("context", "context window 16,384 → 131,072"), ("model_call", f"ollama/{MODEL}")]


# ---- the parts by themselves

def a_chat_of(characters):
    return [context.WireMessage("user", [TextBlock(text="x" * characters)])]


@respx.mock
async def test_the_window_a_call_is_given_is_the_smallest_that_holds_its_request_with_room_to_spare():
    """Room is kept for the answer (a quarter of the window, 16,000 tokens at most), and a request fills 85 percent of
    the rest at most. These are the numbers a request is shortened by: a window that holds it is one nothing is cut for."""
    shown()

    async def given(characters, **options):
        return await context.roomier("ollama", MODEL, "", a_chat_of(characters), Options(**options))
    assert await given(31_000) is None                              # fits 16,384: (16,384 - 4,096) tokens of 3 characters, 85 percent
    assert await given(31_400) == 32_768
    assert await given(62_600) == 32_768                            # (32,768 - 8,192) * 3 * 0.85 = 62,668
    assert await given(62_700) == 65_536
    assert await given(126_300) == 65_536                           # (65,536 - 16,000) * 3 * 0.85 = 126,316
    assert await given(126_400) == 131_072
    assert await given(900_000) == 131_072                          # none holds it: the largest, and the rest is shortened
    assert await given(31_400, context_window=65_536) is None       # the chat's own window holds it
    assert await given(39_000, max_tokens=1_000) is None            # less is kept for an answer the chat limits: (16,384 - 1,000) * 3 * 0.85 = 39,229
    assert await given(39_000, max_tokens=4_000) == 32_768          # (16,384 - 4,000) * 3 * 0.85 = 31,579


@respx.mock
async def test_the_rules_and_tools_are_no_part_of_what_a_larger_window_is_measured_for():
    """They cannot be made smaller and they are in every request: a chat is measured against what they leave."""
    shown()
    rules = "r" * 30_000                                            # with them, 16,384 leaves 6,864 characters for the chat
    assert await context.roomier("ollama", MODEL, rules, a_chat_of(5_000), Options()) is None
    assert await context.roomier("ollama", MODEL, rules, a_chat_of(6_000), Options()) == 32_768     # 85 percent of it: 5,834
    assert await context.roomier("ollama", MODEL, rules, a_chat_of(37_000), Options()) == 32_768    # (73,728 - 30,000) * 0.85 = 37,168
    assert await context.roomier("ollama", MODEL, rules, a_chat_of(37_300), Options()) == 65_536


async def test_a_provider_that_does_not_set_the_window_of_a_call_has_no_larger_one():
    registry.override("abliteration", Down())
    assert await context.roomier("abliteration", "big", "", a_chat_of(5_000_000), Options()) is None
    assert await context.roomier("nobody", "m", "", a_chat_of(5_000_000), Options()) is None


@respx.mock
async def test_the_smallest_size_a_model_did_not_answer_with_is_the_one_that_counts():
    shown()
    p = registry.get("ollama")
    p.too_large(MODEL, 65_536)
    p.too_large(MODEL, 131_072)                                     # a larger one that failed later changes nothing
    assert await p.larger_windows(MODEL, Options()) == [32_768]
    p.too_large(MODEL, 32_768)
    assert await p.larger_windows(MODEL, Options()) == []
    assert await p.larger_windows("another:8b", Options()) == [32_768, 65_536, 131_072]      # each model for itself
