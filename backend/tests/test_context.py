"""A chat that outgrows its model's context window keeps working: old tool results are cut
down first, then the older turns are summarized. What is stored is never changed."""
import json

import pytest

from hunterai import context
from hunterai.providers import registry
from hunterai.providers.base import ModelInfo, ProviderError, Stop, TextDelta, ToolCall, Usage, WireMessage
from hunterai.tools import registry as tools
from hunterai.types import TextBlock
from tests.test_api import BASE, client, new_chat, send  # noqa: F401 (client is a fixture)
from tests.toolkit import call, fake_tool, result


def one_run(n, size=5000):
    """A user message and one assistant message holding n tool calls with their results, as a run stores them."""
    blocks = []
    for i in range(n):
        blocks += [call(f"c{i}", "t", q=i), result(f"c{i}", f"result {i} " + "x" * size)]
    return [WireMessage("user", [TextBlock(text="go")]), WireMessage("assistant", blocks)]


def results_of(wire):
    return [b for m in wire for b in m.blocks if b.type == "tool_result"]


def test_old_tool_results_are_cut_down_oldest_first_until_enough_is_saved():
    wire = one_run(10)
    out, cut = context.stub(wire, need=9000)
    got = results_of(out)
    assert cut == 2 and [len(r.content) < 700 for r in got] == [True, True] + [False] * 8
    assert "result 0" in got[0].content and "5009" in got[0].content        # how it began, and how long it was
    assert [r.tool_call_id for r in got] == [f"c{i}" for i in range(10)]     # every call still has its result
    assert len(results_of(wire)[0].content) == 5009                          # what was passed in is not touched


def test_the_latest_results_and_the_short_ones_are_never_cut():
    wire = one_run(10)
    wire[1].blocks[1] = result("c0", "short")
    out, cut = context.stub(wire, need=10**9)
    got = results_of(out)
    assert cut == 10 - context.KEEP_RESULTS - 1 and got[0].content == "short"
    assert all(len(r.content) == 5009 for r in got[-context.KEEP_RESULTS:])


@pytest.mark.parametrize("message", [
    "HTTP 400: prompt is too long: 205000 tokens > 200000 maximum",
    "HTTP 400: This model's maximum context length is 128000 tokens. However, your messages resulted in 130532 tokens.",
    "HTTP 400: context_length_exceeded",
    "HTTP 400: The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",
    "HTTP 413: Request too large",
])
def test_a_provider_that_refuses_the_size_of_a_request_is_recognised(message):
    assert context.is_overflow(ProviderError("bad_request", message, 400))


@pytest.mark.parametrize("message", [
    "HTTP 400: tools.0.input_schema: invalid",
    "HTTP 400: max_tokens: 99999 > 64000, which is the maximum allowed number of output tokens for this model",
    "HTTP 429: rate limit reached",
])
def test_another_refusal_is_not_taken_for_one_about_size(message):
    assert not context.is_overflow(ProviderError("bad_request", message, 400))


# ---- whole runs, against a model with a small window

CHARS_PER_TOKEN = 4          # how this stand-in model counts; HunterAI must stay under its window without knowing that


class Small:
    """A model with a small context window. It says "noted", writes a summary when it is asked
    for one, calls the tool `t_big` until it has `dig` results, and refuses a request that is
    larger than its window the way a provider does."""
    name = "mock"

    def __init__(self, window, *, known=True, dig=0):
        self.window, self.known, self.dig = window, known, dig
        self.supports_tools = dig > 0
        self.sizes, self.refused, self.summaries = [], 0, 0

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="mock-1", label="mock-1", context_window=self.window if self.known else None)]

    async def stream(self, model, system, messages, options, tools=None):
        size = len(system) + sum(len(getattr(b, "text", None) or "") + len(getattr(b, "content", None) or "")
                                 + (len(json.dumps(b.arguments)) if b.type == "tool_call" else 0) for m in messages for b in m.blocks)
        if size > self.window * CHARS_PER_TOKEN:
            self.refused += 1
            raise ProviderError("bad_request", f"HTTP 400: prompt is too long: {size // CHARS_PER_TOKEN} tokens > {self.window} maximum", 400)
        self.sizes.append(size)
        have = sum(1 for m in messages for b in m.blocks if b.type == "tool_result")
        if system == context.SUMMARY_SYSTEM:
            self.summaries += 1
            yield TextDelta("The user sent filler text and asked for nothing; nothing was decided.")
        elif tools and have < self.dig:
            yield ToolCall(f"c{have}", "t_big", {"n": have})
        else:
            yield TextDelta("noted")
        yield Usage(size // CHARS_PER_TOKEN, 5)
        yield Stop("end")


async def status_of(client, sent):
    run = (await client.get(f"/api/runs/{sent['run_id']}")).json()
    return run["status"], run.get("error")


async def test_a_chat_longer_than_the_window_keeps_answering_and_what_is_stored_stays_whole(client):
    p = Small(10_000); registry.override("mock", p)
    chat = await new_chat(client)
    for n in range(1, 21):                                       # 20 x 3,000 characters: a window and a half
        assert await status_of(client, await send(client, chat["id"], f"message {n}: " + "x" * 3000)) == ("done", None)
    assert p.refused == 0 and max(p.sizes) <= 10_000 * CHARS_PER_TOKEN
    assert 1 <= p.summaries < 10                                 # written when it is needed and used again after that, not once a message
    stored = (await client.get(f"/api/chats/{chat['id']}")).json()["messages"]
    texts = [b["text"] for m in stored if m["role"] == "user" for b in m["blocks"] if b["type"] == "text"]
    assert len(stored) == 40 and [len(t) for t in texts] == [len(f"message {n}: ") + 3000 for n in range(1, 21)]


@pytest.mark.parametrize("extra", [0, 300, 490, 1000, 4000])
async def test_a_summary_is_used_until_the_chat_outgrows_the_room_again(client, monkeypatch, extra):
    """How often a summary is written must not depend on where the sizes happen to fall. With a system text a few
    hundred characters longer, one used to be written for nearly every message: a second model call each time."""
    from hunterai import modes
    monkeypatch.setitem(modes.SYSTEM_PROMPTS, "pentesting", modes.SYSTEM_PROMPTS["pentesting"] + " " + "y" * extra)
    p = Small(10_000); registry.override("mock", p)
    chat = await new_chat(client)
    written = []
    for n in range(1, 21):
        assert await status_of(client, await send(client, chat["id"], f"message {n}: " + "x" * 3000)) == ("done", None)
        written.append(p.summaries)
    assert p.refused == 0 and max(p.sizes) <= 10_000 * CHARS_PER_TOKEN
    assert 1 <= p.summaries <= 6, written
    after = [n for n in range(1, 20) if written[n] > written[n - 1]]       # the messages that needed a new summary
    assert all(b - a >= 2 for a, b in zip(after, after[1:])), written       # never two messages in a row


async def test_inside_one_run_old_tool_results_are_cut_down_and_no_summary_is_needed(client):
    tool, ran = fake_tool("t_big", text="row " + "y" * 15_000); tools.register(tool)
    p = Small(10_000, dig=6); registry.override("mock", p)
    chat = await new_chat(client)
    sent = await send(client, chat["id"], "dig through it")
    assert await status_of(client, sent) == ("done", None)
    assert len(ran) == 6 and p.refused == 0 and p.summaries == 0 and max(p.sizes) <= 10_000 * CHARS_PER_TOKEN
    answer = (await client.get(f"/api/chats/{chat['id']}")).json()["messages"][-1]
    assert [len(b["content"]) for b in answer["blocks"] if b["type"] == "tool_result"] == [15_004] * 6      # stored in full


async def test_a_refusal_for_size_from_a_model_whose_window_is_not_known_is_answered_by_shortening_once(client):
    p = Small(10_000, known=False); registry.override("mock", p)
    chat = await new_chat(client)
    for n in range(1, 9):
        assert await status_of(client, await send(client, chat["id"], f"message {n}: " + "x" * 9000)) == ("done", None)
    assert 1 <= p.refused <= 3 and p.summaries >= 1              # refused when it first outgrew the window, not on every message after


async def test_a_request_that_cannot_be_made_small_enough_says_what_to_do(client):
    p = Small(2_000); registry.override("mock", p)
    chat = await new_chat(client)
    status, error = await status_of(client, await send(client, chat["id"], "x" * 30_000))       # one message, larger than the window
    assert status == "error" and "prompt is too long" in error and "new chat" in error


async def test_claude_models_with_a_small_window_are_not_listed(monkeypatch):
    from hunterai.providers import anthropic as a

    class Listed:
        def __init__(self, id): self.id, self.display_name, self.max_input_tokens = id, id, 1_000_000

    class Client:
        class models:
            @staticmethod
            async def list():
                for i in ("claude-opus-5-5", "claude-opus-4-5-20251101", "claude-haiku-4-5", "claude-sonnet-5", "claude-3-7-sonnet-latest"):
                    yield Listed(i)

    provider = a.AnthropicProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: Client)
    assert [m.id for m in await provider.models()] == ["claude-opus-5-5", "claude-sonnet-5"]
