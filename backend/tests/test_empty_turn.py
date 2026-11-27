"""A model call that ended by itself and wrote nothing a chat can show: no text, no thinking, no tool call. Reported
on 27 November 2026 with DeepHat-V1-7B (Q6_K) on the owner's laptop: after HunterAI's own lookups the chat "just
stopped", while other models answered. The page had a word for a turn of thinking only and none for a turn of nothing.
Ollama is the real provider here, against a stand-in server."""
import httpx
import respx

from hunterai import problems
from hunterai.tools import registry as tools
from hunterai.types import TextBlock, ThinkingBlock, ToolCallBlock
from tests.test_window_grows import (
    answers,
    calls_tool,
    ending,
    go,
    line,
    long_chat,
    shown,
    written,
)
from tests.toolkit import fake_tool

NOTHING = ("The model ended its turn without an answer: it wrote 6 tokens, and no text or tool call came of them. Send the "
           "message again, or pick another model for this chat.")


def nothing(text="", ended="stop", produced=6):
    """What the stand-in Ollama sends for a turn that holds no word: `produced` tokens were written all the same."""
    return httpx.Response(200, text=line({"message": {"role": "assistant", "content": text}, "done": False}) + ending(ended, produced))


def notices(msg):
    return [(b.kind, b.text) for b in msg.blocks if b.type == "notice"]


@respx.mock
async def test_a_turn_that_ends_with_nothing_has_a_word_for_the_owner(conn):
    shown(); answers(nothing())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert notices(msg) == [("no_answer", NOTHING)]
    assert not any(isinstance(b, TextBlock | ThinkingBlock) and b.text.strip() for b in msg.blocks)


@respx.mock
async def test_blank_lines_are_nothing_too(conn):
    shown(); answers(nothing(text="\n\n  \n"))
    chat, user = await long_chat(conn, turns=0)
    _, msg = await go(conn, chat, user)
    assert notices(msg) == [("no_answer", NOTHING)]


@respx.mock
async def test_the_word_counts_what_was_written(conn):
    shown(); answers(nothing(produced=1))
    chat, user = await long_chat(conn, turns=0)
    _, msg = await go(conn, chat, user)
    assert notices(msg) == [("no_answer", ("The model ended its turn without an answer: it wrote 1 token, and no text or tool call "
                                           "came of it. Send the message again, or pick another model for this chat."))]
    shown(); answers(nothing(produced=0))
    chat, user = await long_chat(conn, turns=0)
    _, msg = await go(conn, chat, user)
    assert notices(msg) == [("no_answer", ("The model ended its turn without an answer: it wrote nothing. Send the message again, "
                                           "or pick another model for this chat."))]


@respx.mock
async def test_a_turn_of_nothing_after_a_tool_result_says_so_as_well(conn):
    tool, ran = fake_tool("t_look", text="status: degraded")
    tools.register(tool)
    shown(); answers(calls_tool("t_look"), nothing())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, len(ran), [b.type for b in msg.blocks][:2]) == ("done", 1, ["tool_call", "tool_result"])
    assert notices(msg) == [("no_answer", NOTHING)]


@respx.mock
async def test_an_answer_has_no_such_word(conn):
    shown(); answers(written())
    chat, user = await long_chat(conn, turns=0)
    _, msg = await go(conn, chat, user)
    assert notices(msg) == []


@respx.mock
async def test_a_turn_that_was_cut_off_before_its_first_word_keeps_the_word_for_that(conn):
    shown(); answers(nothing(ended="length", produced=2048))
    chat, user = await long_chat(conn, turns=0, options={"max_tokens": 2048})
    _, msg = await go(conn, chat, user)
    assert [kind for kind, _ in notices(msg)] == ["cut_off"]


def test_what_counts_as_nothing():
    call = ToolCallBlock(id="c1", name="t_look", arguments={})
    assert problems.said_nothing([]) and problems.said_nothing([TextBlock(text=" \n\n")])
    assert not problems.said_nothing([TextBlock(text="degraded")])
    assert not problems.said_nothing([ThinkingBlock(text="The id stands in the path.")])
    assert not problems.said_nothing([call]) and not problems.said_nothing([TextBlock(text=""), call])
