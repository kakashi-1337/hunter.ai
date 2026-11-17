"""A local model gets a request that fits the window it is given. The case that was reported:
the chat's own model cannot be reached, the local one answers in its place, and it has to know
what the chat was about. Ollama is the real provider here, against a stand-in server."""
import json

import httpx
import pytest
import respx

from hunterai import context, db, engine
from hunterai.providers import ollama, registry
from hunterai.providers.base import ModelInfo, ProviderError
from hunterai.repo import chats, messages, runs
from hunterai.tools import native
from hunterai.types import TextBlock

HOST = "http://127.0.0.1:11434"
SUMMARY = "The user is mapping the login of shop.example. /admin answered 403; /api/v2/users lists accounts without a session."
ANSWER = "Next I would try the account list with another user's id."
LATEST = "What should we try next on that endpoint?"


class Down:
    """The chat's own model, with a large window: it cannot be reached."""
    name = "abliteration"
    supports_tools = True

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="big", label="big", context_window=262144)]

    async def stream(self, model, system, msgs, options, tools=None):
        raise ProviderError("unavailable", "the provider cannot be reached", 503)
        yield       # a generator, like every provider's stream


def line(obj):
    return json.dumps(obj) + "\n"


def local_model(context_length=40960):
    """A stand-in Ollama: it writes SUMMARY when it is asked for a summary and ANSWER otherwise.
    Returns the route of /api/chat, which holds every request it got."""
    def reply(request):
        first = json.loads(request.content)["messages"][0]
        says = SUMMARY if first["role"] == "system" and first["content"] == context.SUMMARY_SYSTEM else ANSWER
        return httpx.Response(200, text=line({"message": {"role": "assistant", "content": says}, "done": False})
                              + line({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                                      "prompt_eval_count": 900, "eval_count": 12}))
    respx.post(HOST + "/api/show").mock(return_value=httpx.Response(200, json={
        "capabilities": ["completion", "tools"],
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": context_length}}))
    return respx.post(HOST + "/api/chat").mock(side_effect=reply)


def bodies(route):
    """(the requests for a summary, the requests for an answer), as Ollama got them."""
    got = [json.loads(c.request.content) for c in route.calls]
    is_summary = lambda b: b["messages"][0]["content"] == context.SUMMARY_SYSTEM      # noqa: E731
    return [b for b in got if is_summary(b)], [b for b in got if not is_summary(b)]


def said(body) -> str:
    return "\n".join(m.get("content") or "" for m in body["messages"] if m["role"] != "system")


async def long_chat(conn, turns=14, size=3000):
    """A chat with the cloud model that has gone on for a while: `turns` questions and answers of `size`
    characters each, then one more question. Returns (chat, the last user message)."""
    chat = await chats.create(conn, None, "pentesting", "abliteration", "big", {})
    parent = None
    for n in range(1, turns + 1):
        user = await messages.add(conn, chat.id, parent, "user", [TextBlock(text=f"question {n}: " + "words " * (size // 6))])
        reply = await messages.add(conn, chat.id, user.id, "assistant", [TextBlock(text=f"answer {n}: " + "found " * (size // 6))],
                                   provider="abliteration", model="big")
        parent = reply.id
    return chat, await messages.add(conn, chat.id, parent, "user", [TextBlock(text=LATEST)])


async def no_bytes(upload_id):
    return b""


async def go(conn, chat, user):
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    return await runs.get(conn, run_id), await messages.get(conn, mid)


@respx.mock
async def test_a_local_model_that_answers_in_place_of_the_chats_model_still_knows_the_chat(conn):
    registry.override("abliteration", Down())
    route = local_model()
    chat, user = await long_chat(conn)                  # about 84,000 characters: five times the local window's room
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert (msg.provider, msg.fallback_from, msg.blocks[-1].text) == ("ollama", "abliteration/big", ANSWER)
    summaries, answers = bodies(route)
    assert len(answers) == 1
    sent = answers[0]
    assert sent["options"]["num_ctx"] == ollama.DEFAULT_NUM_CTX
    assert ollama._chars(sent) / ollama.CHARS_PER_TOKEN <= ollama.DEFAULT_NUM_CTX * 0.75        # room is left for the answer
    # What it is given: a summary of the early part, the latest turns word for word, and the question itself.
    assert context.SUMMARY_MARK in said(sent) and SUMMARY in said(sent)
    assert "question 1:" not in said(sent) and "answer 14:" in said(sent)
    assert said(sent).rstrip().endswith(LATEST)
    assert len(summaries) == 1 and "question 1:" in said(summaries[0]) and "answer 11:" in said(summaries[0])
    assert [(s.kind, s.status) for s in run.steps] == [
        ("model_call", "error"), ("fallback", "done"), ("model_call", "done"), ("context", "done")]
    assert run.steps[2].detail["context"]["messages_summarized"] == 22


@respx.mock
async def test_with_the_tools_of_a_real_chat_the_request_still_fits_and_carries_the_chat(conn):
    """HunterAI's own rules and tool definitions are most of what a local model is sent."""
    native.register_all()
    registry.override("abliteration", Down())
    route = local_model()
    chat, user = await long_chat(conn)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error, msg.provider) == ("done", None, "ollama")
    _, answers = bodies(route)
    sent = answers[-1]
    assert len(sent["tools"]) > 20 and sent["truncate"] is False and sent["shift"] is False
    assert ollama._chars(sent) / ollama.CHARS_PER_TOKEN <= ollama.DEFAULT_NUM_CTX * 0.75        # room is left for the answer
    # Beside the rules and tools there is still room for a summary of the early part and the latest turn as it was said.
    assert SUMMARY in said(sent) and "question 14:" in said(sent) and "answer 14:" in said(sent)
    assert "answer 12:" not in said(sent) and said(sent).rstrip().endswith(LATEST)


@respx.mock
async def test_a_short_chat_is_sent_to_the_local_model_whole(conn):
    registry.override("abliteration", Down())
    route = local_model()
    chat, user = await long_chat(conn, turns=2, size=600)
    run, _ = await go(conn, chat, user)
    summaries, answers = bodies(route)
    assert run.status == "done" and summaries == []
    assert [m["role"] for m in answers[0]["messages"]] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert "question 1:" in said(answers[0])


@respx.mock
async def test_a_window_too_small_for_the_rules_and_tools_is_said_and_nothing_is_sent_cut(conn, monkeypatch):
    monkeypatch.setenv(ollama.NUM_CTX_ENV, "4096")
    native.register_all()
    registry.override("abliteration", Down())
    route = local_model()
    chat, user = await long_chat(conn, turns=2, size=600)
    run, _ = await go(conn, chat, user)
    assert run.status == "error"
    assert "does not fit the context window of the local model" in run.error and "4,096" in run.error
    assert "Context window for local models" in run.error
    _, answers = bodies(route)
    assert answers == []                                 # Ollama was never handed a request it would have cut


@respx.mock
async def test_a_refusal_by_ollama_for_size_is_answered_by_a_smaller_request(conn):
    """The count of characters is a guess at the tokens: when Ollama says the request is too large after all,
    the chat is shortened further and sent once more."""
    registry.override("abliteration", Down())
    refused = []

    def reply(request):
        body = json.loads(request.content)
        if body["messages"][0]["content"] != context.SUMMARY_SYSTEM and not refused:
            refused.append(ollama._chars(body))
            return httpx.Response(400, json={"error": "request (17000 tokens) exceeds the available context size (16384 tokens), try increasing it"})
        says = SUMMARY if body["messages"][0]["content"] == context.SUMMARY_SYSTEM else ANSWER
        return httpx.Response(200, text=line({"message": {"role": "assistant", "content": says}, "done": False})
                              + line({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"}))
    respx.post(HOST + "/api/show").mock(return_value=httpx.Response(200, json={"capabilities": ["completion"]}))
    route = respx.post(HOST + "/api/chat").mock(side_effect=reply)
    chat, user = await long_chat(conn, turns=4, size=3000)      # 24,000 characters: within the room by HunterAI's count
    run, msg = await go(conn, chat, user)
    assert (run.status, msg.blocks[-1].text) == ("done", ANSWER)
    _, answers = bodies(route)
    assert len(answers) == 2 and ollama._chars(answers[1]) < refused[0] / 1.5
    assert said(answers[1]).rstrip().endswith(LATEST)
