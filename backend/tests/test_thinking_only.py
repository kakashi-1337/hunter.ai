"""A model call that ended with thinking only. Seen on the owner's laptop on 26 November 2026 with
Huihui-Qwen3.5-9B-abliterated, thinking on: its turn after a tool result was thinking and nothing else, so a chat got
no answer and the bench counted 2 passes of 60. With thinking off the same model answered. HunterAI asks such a turn
once more with thinking off. Ollama is the real provider here, against a stand-in server."""
import httpx
import pytest
import respx

from hunterai import problems
from hunterai.providers.base import WireMessage, asked_again
from hunterai.repo import chats
from hunterai.tools import registry as tools
from hunterai.types import TextBlock, ThinkingBlock, ToolCallBlock
from tests.test_window_grows import (
    ANSWER,
    MODEL,
    answers,
    bodies,
    calls_tool,
    ending,
    events,
    go,
    line,
    long_chat,
    shown,
    written,
)
from tests.toolkit import fake_tool

THOUGHT = "The id stands in the path. So the next thing to try is another user's id."
CALL = ("model_call", f"ollama/{MODEL}", "done")
AGAIN = ("thinking", "ended with thinking only: asked again with thinking off", "done")
CARRIED = ("Your last turn ended after thinking, with no answer and no tool call. This is what you thought:\n\n"
           f"{THOUGHT}\n\nGo on from it: write your answer now, or make the tool call you decided on.")


def thought(text=THOUGHT, ended="stop"):
    """What the stand-in Ollama sends for a turn that is thinking and nothing else."""
    return httpx.Response(200, text=line({"message": {"role": "assistant", "content": "", "thinking": text}, "done": False}) + ending(ended, 40))


def steps(run):
    return [(s.kind, s.name, s.status) for s in run.steps]


# ---- what counts as thinking only

@pytest.mark.parametrize("blocks, stop", [
    ([ThinkingBlock(text="hm")], "stop"), ([ThinkingBlock(text="hm")], "end"), ([ThinkingBlock(text="hm")], "end_turn"),
    ([ThinkingBlock(text="hm")], None),
    ([ThinkingBlock(text="hm"), TextBlock(text=" \n")], "stop"),            # white space is no answer
    ([ThinkingBlock(text=""), ThinkingBlock(text="hm")], "stop"),
])
def test_thinking_and_nothing_after_it_is_thinking_only(blocks, stop):
    assert problems.only_thought(blocks, stop) is True


@pytest.mark.parametrize("blocks, stop", [
    ([ThinkingBlock(text="hm"), TextBlock(text="ok")], "stop"),
    ([TextBlock(text="ok")], "stop"),
    ([ThinkingBlock(text="hm"), ToolCallBlock(id="a", name="t", arguments={})], "stop"),
    ([], "stop"), ([ThinkingBlock(text=" \n")], "stop"),                    # a turn with nothing in it is another matter
    ([ThinkingBlock(text="hm")], "length"), ([ThinkingBlock(text="hm")], "max_tokens"),     # cut off: it has its own word
    ([ThinkingBlock(text="hm")], "MAX_TOKENS"),
    ([ThinkingBlock(text="hm")], "content_filter"), ([ThinkingBlock(text="hm")], "model_context_window_exceeded"),
    ([ThinkingBlock(text="hm", opaque={"signature": "x"})], "end_turn"),    # sealed by its provider: replayed as it came
])
def test_an_answer_a_call_a_cut_or_sealed_thinking_is_not(blocks, stop):
    assert problems.only_thought(blocks, stop) is False


# ---- what the second asking carries

def test_the_second_asking_is_given_what_the_model_thought_and_the_word_to_go_on():
    assert problems.second_asking([ThinkingBlock(text=THOUGHT)]) == CARRIED
    assert problems.second_asking([ThinkingBlock(text=f"  {THOUGHT}\n"), TextBlock(text=" ")]) == CARRIED


def test_the_thinking_of_several_blocks_is_carried_in_their_order():
    said = problems.second_asking([ThinkingBlock(text="First the path."), ThinkingBlock(text=" "), ThinkingBlock(text="Then the id.")])
    assert "This is what you thought:\n\nFirst the path.\n\nThen the id.\n\nGo on from it" in said


def test_of_a_long_thought_the_end_is_carried_where_its_conclusion_stands():
    long = "A" * 5000 + "B" * problems.THOUGHT_KEPT
    said = problems.second_asking([ThinkingBlock(text=long)])
    assert "This is the end of what you thought:\n\n…" + "B" * problems.THOUGHT_KEPT + "\n\nGo on from it" in said and "A" not in said
    exact = problems.second_asking([ThinkingBlock(text="B" * problems.THOUGHT_KEPT)])
    assert "This is what you thought:\n\nBBB" in exact and "…" not in exact


def test_thinking_without_words_gives_the_second_asking_nothing_to_carry():
    assert problems.second_asking([]) is None
    assert problems.second_asking([ThinkingBlock(text=" \n"), TextBlock(text="an answer")]) is None


# ---- a chat

@respx.mock
async def test_a_turn_that_ended_with_thinking_only_is_asked_once_more_with_thinking_off(conn):
    shown()
    route = answers(thought(), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    first, second = bodies(route)
    assert "think" not in first and second["think"] is False
    assert second["messages"] == first["messages"] + [{"role": "user", "content": CARRIED}]    # the same question, and what it thought
    assert [b.type for b in msg.blocks] == ["thinking", "text", "notice"]
    assert (msg.blocks[0].text, msg.blocks[1].text) == (THOUGHT, ANSWER)    # what it thought stays above the answer
    assert (msg.blocks[2].kind, msg.blocks[2].text) == ("thinking", problems.ASKED_AGAIN)
    assert steps(run) == [CALL, AGAIN, CALL]
    assert (await chats.get(conn, chat.id)).options == {}           # the chat's own options are the owner's to change
    assert await events(run, "thinking_delta", "text_delta", "answer_restarted") == [
        {"type": "thinking_delta", "text": THOUGHT}, {"type": "text_delta", "text": ANSWER}]


def test_the_word_under_such_an_answer_says_where_the_switch_is():
    assert problems.ASKED_AGAIN == (
        "The model ended a turn with thinking only and no answer, so HunterAI asked it once more with thinking switched off, "
        "and gave it what it had thought. Thinking stays off for the rest of this run. To have it off from the start, switch Thinking off for this chat under Options.")


@respx.mock
async def test_the_turn_after_a_tool_result_is_asked_again_and_the_tool_is_not_run_twice(conn):
    tool, ran = fake_tool("t_look", text="uid=1000")
    tools.register(tool)
    shown()
    route = answers(calls_tool("t_look", q=1), thought(), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    one, two, three = bodies(route)
    assert "think" not in one and "think" not in two and three["think"] is False
    assert three["messages"] == two["messages"] + [{"role": "user", "content": CARRIED}]
    assert ran == [{"q": 1}]
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_result", "thinking", "text", "notice"]
    assert msg.blocks[3].text == ANSWER


@respx.mock
async def test_only_the_thinking_of_the_turn_that_gave_no_answer_is_carried(conn):
    tool, _ = fake_tool("t_look", text="uid=1000")
    tools.register(tool)
    shown()
    earlier = httpx.Response(200, text=line({"message": {"role": "assistant", "content": "", "thinking": "First I look.", "tool_calls": [
        {"function": {"name": "t_look", "arguments": {"q": 1}}}]}, "done": False}) + ending("stop", 12))
    route = answers(earlier, thought(), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    carried = bodies(route)[2]["messages"][-1]
    assert carried == {"role": "user", "content": CARRIED} and "First I look." not in carried["content"]
    assert [b.type for b in msg.blocks] == ["thinking", "tool_call", "tool_result", "thinking", "text", "notice"]


def test_a_second_asking_with_nothing_to_carry_is_the_same_question():
    wire = [WireMessage("user", [TextBlock(text="q")])]
    assert asked_again(wire, None) is wire and asked_again(wire, "") is wire
    assert asked_again(wire, "what was thought") == [*wire, WireMessage("user", [TextBlock(text="what was thought")])] and len(wire) == 1


@respx.mock
async def test_thinking_stays_off_for_the_rest_of_the_run(conn):
    tool, ran = fake_tool("t_look", text="uid=1000")
    tools.register(tool)
    shown()
    route = answers(thought(), calls_tool("t_look", q=1), written())
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    one, two, three = bodies(route)
    assert ("think" in one, two["think"], three["think"]) == (False, False, False)
    assert ran == [{"q": 1}]                                        # the call the second try made is run
    assert [b.type for b in msg.blocks] == ["thinking", "tool_call", "tool_result", "text", "notice"]
    assert steps(run).count(AGAIN) == 1


@respx.mock
async def test_a_chat_with_thinking_switched_off_is_not_asked_again(conn):
    shown()
    route = answers(thought())
    chat, user = await long_chat(conn, turns=0, options={"thinking": False})
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert [b["think"] for b in bodies(route)] == [False]
    assert [b.type for b in msg.blocks] == ["thinking", "notice"]
    assert (msg.blocks[1].kind, msg.blocks[1].text) == ("no_answer", (
        "The model ended its turn with thinking only and gave no answer. What it thought stands above. Send the message again, "
        "or pick another model for this chat."))
    assert steps(run) == [CALL]


@respx.mock
async def test_a_second_turn_of_thinking_only_is_left_as_it_is_and_says_so(conn):
    shown()
    route = answers(thought(), thought(" Still not sure."))
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert len(bodies(route)) == 2                                  # asked again once, not for as long as it takes
    assert [b.type for b in msg.blocks] == ["thinking", "notice"]
    assert msg.blocks[0].text == THOUGHT + " Still not sure."
    assert (msg.blocks[1].kind, msg.blocks[1].text) == ("no_answer", (
        "The model ended its turn with thinking only and gave no answer, also when it was asked once more with thinking "
        "switched off. What it thought stands above. Send the message again, or pick another model for this chat."))
    assert steps(run) == [CALL, AGAIN, CALL]


@respx.mock
async def test_a_second_call_the_server_refuses_does_not_fail_the_run(conn):
    shown()
    route = answers(thought(), httpx.Response(400, json={"error": "think value false is not supported for this model"}))
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error) == ("done", None)
    assert len(bodies(route)) == 2
    assert [b.type for b in msg.blocks] == ["thinking", "notice"]
    assert msg.blocks[1].kind == "no_answer"
    assert msg.blocks[1].text.startswith(
        "The model ended its turn with thinking only and gave no answer. HunterAI asked it once more with thinking switched off, "
        "and that call failed (")
    assert "think value false is not supported for this model" in msg.blocks[1].text
    assert msg.blocks[1].text.endswith("What it thought stands above. Send the message again, or pick another model for this chat.")
    assert steps(run) == [CALL, AGAIN, ("model_call", f"ollama/{MODEL}", "error")]
    assert run.steps[0].detail["stop_reason"] == "stop"


@respx.mock
async def test_a_turn_after_a_tool_result_that_stays_thinking_only_says_so_too(conn):
    tool, ran = fake_tool("t_look", text="uid=1000")
    tools.register(tool)
    shown()
    route = answers(calls_tool("t_look", q=1), thought(), thought(" Still not sure."))
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error, len(bodies(route))) == ("done", None, 3)
    assert ran == [{"q": 1}]
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_result", "thinking", "notice"]
    assert (msg.blocks[3].kind, msg.blocks[3].text) == ("no_answer", problems.no_answer(True))


@respx.mock
async def test_a_second_call_that_breaks_after_it_began_to_answer_is_the_failure_any_broken_answer_is(conn):
    shown()
    broke = httpx.Response(200, text=line({"message": {"role": "assistant", "content": "Next I would"}, "done": False})
                           + line({"error": "the model was unloaded"}))
    route = answers(thought(), broke)
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert len(bodies(route)) == 2
    assert run.status == "error" and "the model was unloaded" in run.error
    assert [b.type for b in msg.blocks][:2] == ["thinking", "text"] and msg.blocks[1].text == "Next I would"


@respx.mock
async def test_thinking_that_was_cut_off_at_a_limit_is_not_asked_again_and_keeps_its_own_word(conn):
    shown()
    cut = httpx.Response(200, text=line({"message": {"role": "assistant", "content": "", "thinking": THOUGHT}, "done": False}) + ending("length", 100))
    route = answers(cut, written())
    chat, user = await long_chat(conn, turns=0, options={"max_tokens": 100})
    run, msg = await go(conn, chat, user)
    assert (run.status, len(bodies(route))) == ("done", 1)
    assert [b.type for b in msg.blocks] == ["thinking", "notice"]
    assert msg.blocks[1].kind == "cut_off" and "100 output tokens this chat allows" in msg.blocks[1].text
    assert steps(run) == [CALL]


@respx.mock
async def test_a_run_that_was_asked_again_and_ends_without_an_answer_says_both(conn):
    tool, ran = fake_tool("t_look", text="uid=1000")
    tools.register(tool)
    shown()
    route = answers(thought(), calls_tool("t_look", q=1), thought(" And now?"))
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert (run.status, run.error, len(bodies(route))) == ("done", None, 3)
    assert ran == [{"q": 1}]
    assert [b.type for b in msg.blocks] == ["thinking", "tool_call", "tool_result", "thinking", "notice", "notice"]
    assert [(b.kind, b.text) for b in msg.blocks[4:]] == [("thinking", problems.ASKED_AGAIN), ("no_answer", problems.no_answer(True))]


def test_what_a_refused_second_call_said_is_shown_in_part():
    said = problems.no_answer(True, "x" * 400)
    assert "(" + "x" * 300 + ")" in said


@respx.mock
async def test_an_answer_that_came_is_left_alone(conn):
    shown()
    route = answers(httpx.Response(200, text=line({"message": {"role": "assistant", "content": "", "thinking": THOUGHT}, "done": False})
                                   + line({"message": {"role": "assistant", "content": ANSWER}, "done": False}) + ending("stop", 40)))
    chat, user = await long_chat(conn, turns=0)
    run, msg = await go(conn, chat, user)
    assert len(bodies(route)) == 1
    assert [b.type for b in msg.blocks] == ["thinking", "text"]
    assert steps(run) == [CALL]
