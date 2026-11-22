"""A chat that outgrows its model's context window keeps working: old tool results are cut
down first, then the older turns are summarized. What is stored is never changed."""
import json

import pytest

from hunterai import context
from hunterai.providers import ollama, registry
from hunterai.providers.base import ModelInfo, Options, ProviderError, Stop, TextDelta, ToolCall, ToolSpec, Usage, WireMessage
from hunterai.repo import queue
from hunterai.tools import registry as tools
from hunterai.types import TextBlock
from tests.test_api import BASE, client, new_chat, send  # noqa: F401 (client is a fixture)
from tests.toolkit import call, fake_tool, result, seeded_run


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


CODE = "0123456789ab"                       # the code of a chat, as repo/queue.py makes one
SAID = "stop digging there.\n\nWrite up what you have."


def noted(text, code=CODE, said=SAID):
    """A tool result with what the owner sent during the work at its end, as the engine hands it to the model."""
    return text + queue.block(code, [said])


def test_what_the_owner_sent_during_the_work_stays_when_its_tool_result_is_cut_down():
    """Reported: in a long chat with a local model the owner's latest messages were not followed. A message sent
    during the work stands at the end of a tool result, and a result that is cut down keeps how it began."""
    wire = one_run(10)
    wire[1].blocks[3] = result("c1", noted("result 1 " + "x" * 5000))
    out, cut = context.stub(wire, need=10**9, owner=CODE)
    got = results_of(out)[1].content
    assert cut == 10 - context.KEEP_RESULTS
    assert got.startswith(context.STUB_MARK) and "5009 characters" in got and "result 1 xxx" in got      # the tool's own part, cut down
    assert got.endswith(queue.block(CODE, [SAID])) and got.count("OWNER_MESSAGE") == 2                   # the owner's words, whole and once
    assert len(got) < 800 and len(results_of(wire)[1].content) > 5000                                    # and nothing passed in is changed
    again, more = context.stub(out, need=10**9, owner=CODE)
    assert more == 0 and results_of(again)[1].content == got


def test_only_this_chats_code_marks_the_owners_words():
    """A page or a file can end with lines that look the same. Without the code they are tool output like the
    rest, and a result that is cut down keeps none of it."""
    for owner, code in ((CODE, "ffffffffffff"), (None, CODE), ("", CODE)):
        wire = one_run(10)
        wire[1].blocks[3] = result("c1", noted("result 1 " + "x" * 5000, code=code))
        got = results_of(context.stub(wire, need=10**9, owner=owner)[0])[1].content
        assert got.startswith(context.STUB_MARK) and "stop digging" not in got and len(got) < 700, (owner, code)


def test_the_owners_block_is_the_last_one_of_a_result():
    """A command can print the line that opens such a block (a model that echoes what it was sent). What the
    engine added is what stands at the very end."""
    echoed = "the log says:" + queue.block(CODE, ["something a tool printed"])[:-len("<<<END_OWNER_MESSAGE 0123456789ab>>>")] + "and more output"
    own, said = queue.split(noted(echoed), CODE)
    assert own == echoed and said == SAID
    assert queue.split(echoed, CODE) == (echoed, None)                      # opened and never closed at the end: tool output
    assert queue.split(noted("out"), None) == (noted("out"), None) and queue.split("", CODE) == ("", None)
    assert queue.split(queue.block(CODE, ["only this"]), CODE) == ("", "only this")


def test_a_short_result_with_a_long_message_from_the_owner_is_left_as_it_is():
    wire = one_run(10)
    whole = noted("exit code: 0", said="read this first: " + "w" * 3000)
    wire[1].blocks[3] = result("c1", whole)
    out, cut = context.stub(wire, need=10**9, owner=CODE)
    assert results_of(out)[1].content == whole and cut == 10 - context.KEEP_RESULTS - 1


def test_the_summary_is_told_what_the_owner_sent_during_the_work():
    wire = [WireMessage("user", [TextBlock(text="map the login")]),
            WireMessage("assistant", [call("c1", "t", q=1), result("c1", noted("found /admin " + "x" * 5000)), TextBlock(text="mapped")])]
    told = context._told(wire, 10**6, owner=CODE)
    assert "[the user wrote this while the assistant worked: stop digging there.\n\nWrite up what you have.]" in told
    assert told.index("found /admin") < told.index("stop digging") < told.index("mapped") and "OWNER_MESSAGE" not in told
    assert "stop digging" not in context._told(wire, 10**6, owner="ffffffffffff")       # another chat's code: tool output, and cut like it


@pytest.mark.parametrize("message", [
    "HTTP 400: prompt is too long: 205000 tokens > 200000 maximum",
    "HTTP 400: This model's maximum context length is 128000 tokens. However, your messages resulted in 130532 tokens.",
    "HTTP 400: context_length_exceeded",
    "HTTP 400: The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",
    "HTTP 413: Request too large",
    "HTTP 400: request (9000 tokens) exceeds the available context size (8192 tokens), try increasing it",      # Ollama
    "HTTP 400: the prompt is longer than the context length currently available to the model; shorten the prompt",
    ollama.TOO_LARGE.format(model="qwen3:8b", need=9000, window=8192),         # HunterAI's own, before it sends
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


async def test_a_provider_that_sets_the_window_of_each_call_is_the_one_asked_for_it():
    """A local model has the window HunterAI gives it, which is not what a model list says."""
    class Local:
        name = "ollama"

        async def window_for(self, model, options):
            return min(options.context_window or 16384, 8192 if model == "small" else 131072)

        async def models(self):
            raise AssertionError("the model list does not say what a call gets")
    registry.override("ollama", Local())
    assert await context.window("ollama", "small", Options()) == 8192
    assert await context.window("ollama", "small", Options(context_window=65536)) == 8192
    assert await context.window("ollama", "large", Options()) == 16384
    assert await context.window("ollama", "large", Options(context_window=4096)) == 4096


async def test_a_window_that_cannot_be_asked_for_falls_back_on_the_chats_own():
    class Broken:
        name = "ollama"

        async def window_for(self, model, options):
            raise RuntimeError("no answer")
    registry.override("ollama", Broken())
    assert await context.window("ollama", "m", Options()) is None
    assert await context.window("ollama", "m", Options(context_window=4096)) == 4096


async def test_the_room_for_the_chat_is_what_the_rules_and_the_tools_leave(conn):
    """A local model's window is small, and HunterAI's rules and tool definitions fill most of it. What they leave
    is for the chat: the latest turns stay word for word while they fit there. Measured against the whole window,
    a long chat was cut down to its last message while a third of the window stayed empty."""
    registry.override("mock", Small(10_000))                                 # room: (10,000 - 2,500) x 3 = 22,500 characters
    _, run_id = await seeded_run(conn, provider="mock")
    specs = [ToolSpec(f"tool_{i:02}", "d" * 660, {"type": "object"}) for i in range(20)]      # 13,700 of them
    wire = []
    for n in range(1, 7):
        wire += [WireMessage("user", [TextBlock(text=f"question {n}: " + "q" * 1000)]),
                 WireMessage("assistant", [TextBlock(text=f"answer {n}: " + "a" * 1000)])]
    wire.append(WireMessage("user", [TextBlock(text="and now?")]))
    out, note = await context.fit(conn, run_id, "mock", "mock-1", "rules", wire, Options(), specs)
    kept = "\n".join(b.text for m in out for b in m.blocks if b.type == "text")
    assert context.SUMMARY_MARK in kept and kept.endswith("and now?")
    assert "question 6:" in kept and "answer 6:" in kept and "answer 5:" not in kept
    assert context.size("rules", out, specs) <= 22_500 * context.HEADROOM
    assert note["messages_summarized"] == 10


async def test_a_summary_the_chat_already_has_is_enough_while_the_rest_fits_what_is_left(conn):
    """No second summary, with a model call of its own, for a chat that fits beside the rules and tools once its
    stored summary stands in for the early part."""
    from hunterai.repo import messages, summaries
    p = Small(10_000); registry.override("mock", p)
    chat, run_id = await seeded_run(conn, provider="mock")
    specs = [ToolSpec(f"tool_{i:02}", "d" * 660, {"type": "object"}) for i in range(20)]      # 13,700 characters; 8,800 are left
    wire, parent = [], None
    for role, size in [("user", 1000), ("assistant", 1000), ("user", 1000), ("assistant", 1000),
                       ("user", 2500), ("assistant", 2500), ("user", 1000)]:
        m = await messages.add(conn, chat.id, parent, role, [TextBlock(text=role[0] * size)])
        wire.append(WireMessage(role, list(m.blocks), id=m.id))
        parent = m.id
    await summaries.put(conn, chat.id, wire[3].id, "They mapped the login and found /admin.")
    out, note = await context.fit(conn, run_id, "mock", "mock-1", "rules", wire, Options(), specs)
    assert p.summaries == 0 and (note["summary_used"], note["messages_summarized"]) == (True, 0)
    assert len(out) == 3 and "They mapped the login and found /admin." in out[0].blocks[0].text
    assert 22_500 * context.HEADROOM < context.size("rules", out, specs) <= 22_500      # over 85% of the room, and within it


async def test_a_chat_that_fits_what_the_rules_and_the_tools_leave_is_sent_whole(conn):
    registry.override("mock", Small(10_000))
    _, run_id = await seeded_run(conn, provider="mock")
    specs = [ToolSpec(f"tool_{i:02}", "d" * 660, {"type": "object"}) for i in range(20)]
    wire = [WireMessage("user", [TextBlock(text="q" * 3000)]), WireMessage("assistant", [TextBlock(text="a" * 3000)]),
            WireMessage("user", [TextBlock(text="and now?")])]                 # 6,000 characters where 8,800 are left
    out, note = await context.fit(conn, run_id, "mock", "mock-1", "rules", wire, Options(), specs)
    assert out is wire and note is None


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


class Listening(Small):
    """A small model that also keeps, for each call, whether the request held the owner's words."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.heard = []

    async def stream(self, model, system, messages, options, tools=None):
        if system != context.SUMMARY_SYSTEM:
            self.heard.append(any("write up what you have" in (getattr(b, "content", None) or "") for m in messages for b in m.blocks))
        async for ev in super().stream(model, system, messages, options, tools):
            yield ev


async def test_a_message_sent_during_a_long_run_is_in_every_request_after_it(client):
    """The whole of it: a model with a small window digs through eight large results, and the owner writes while
    the second is being fetched. Each later request is cut down to fit, and each still has what the owner wrote."""
    import dataclasses
    from hunterai import db
    from hunterai.tools.base import ToolResult
    ran = []

    async def dig(ctx, args):
        ran.append(args)
        if len(ran) == 2:
            conn = await db.connect()
            try:
                await queue.add(conn, ctx.chat_id, "stop digging, write up what you have")
            finally:
                await conn.close()
        return ToolResult("row " + "y" * 15_000)
    tool, _ = fake_tool("t_big"); tools.register(dataclasses.replace(tool, handler=dig))
    p = Listening(10_000, dig=8); registry.override("mock", p)
    chat = await new_chat(client)
    sent = await send(client, chat["id"], "dig through it")
    assert await status_of(client, sent) == ("done", None)
    assert len(ran) == 8 and p.refused == 0 and max(p.sizes) <= 10_000 * CHARS_PER_TOKEN
    assert p.heard == [False, False] + [True] * 7, p.heard


async def test_a_summary_of_the_earlier_turns_carries_what_the_owner_sent_during_them(client):
    """Later turns: the run in which the owner wrote is among the older messages now, and a summary stands in
    for them. Whoever writes it is given the owner's words as the user's."""
    import dataclasses
    from hunterai import db
    from hunterai.tools.base import ToolResult
    asked = []

    class Told(Small):
        async def stream(self, model, system, messages, options, tools=None):
            if system == context.SUMMARY_SYSTEM:
                asked.append(messages[0].blocks[0].text)
            async for ev in super().stream(model, system, messages, options, tools):
                yield ev

    async def dig(ctx, args):
        if args["n"] == 0:
            conn = await db.connect()
            try:
                await queue.add(conn, ctx.chat_id, "leave the admin panel alone")
            finally:
                await conn.close()
        return ToolResult("row " + "y" * 3000)
    tool, _ = fake_tool("t_big"); tools.register(dataclasses.replace(tool, handler=dig))
    p = Told(10_000, dig=2); registry.override("mock", p)
    chat = await new_chat(client)
    assert await status_of(client, await send(client, chat["id"], "dig through it")) == ("done", None)
    p.dig = 0                                                    # from here on it only answers
    for n in range(1, 13):
        assert await status_of(client, await send(client, chat["id"], f"message {n}: " + "x" * 3000)) == ("done", None)
    assert p.summaries >= 1 and p.refused == 0
    assert "[the user wrote this while the assistant worked: leave the admin panel alone]" in asked[0]


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
    monkeypatch.setattr(provider, "_get_client", lambda via=None: Client)
    assert [m.id for m in await provider.models()] == ["claude-opus-5-5", "claude-sonnet-5"]
