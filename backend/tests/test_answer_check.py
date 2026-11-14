import pytest

from hunterai import answer_check, engine
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import messages, runs, settings
from hunterai.tools import registry
from hunterai.types import TextBlock, ThinkingBlock, ToolCallBlock, ToolResultBlock
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, fake_tool


@pytest.fixture
def on(monkeypatch):
    monkeypatch.delenv("HUNTERAI_ANSWER_CHECK")


def judged(monkeypatch, answer, error=None):
    """The decision model answers `answer`; what it was asked is returned."""
    asked = []

    async def complete_json(conn, system, user, schema, **kw):
        asked.append({"system": system, "user": user, "schema": schema})
        meta = {"provider": "anthropic", "model": "haiku", "ms": 5, "usage": {}}
        return (None, {**meta, "error": error}) if error else (answer, meta)
    monkeypatch.setattr(answer_check.de, "complete_json", complete_json)
    return asked


ITEMS = [ThinkingBlock(text="hmm"), TextBlock(text="Let me scan."),
         ToolCallBlock(id="c1", name="shell", arguments={"command": "nmap -p22,80 a.test"}),
         ToolResultBlock(tool_call_id="c1", content="22/tcp open ssh\n80/tcp closed http"),
         TextBlock(text="Port 22 is open and port 80 is open too. I also confirmed the admin panel has default credentials.")]


def test_the_material_is_the_last_answer_and_the_tool_results_of_the_run():
    answer, evidence = answer_check.material(ITEMS)
    assert answer.startswith("Port 22 is open") and "Let me scan" not in answer
    assert 'shell {"command": "nmap -p22,80 a.test"}' in evidence and "80/tcp closed http" in evidence and "hmm" not in evidence
    assert answer_check.material([TextBlock(text="Just an explanation.")]) == ("Just an explanation.", "")
    big = [ToolCallBlock(id=f"c{i}", name="read", arguments={}) for i in range(40)]
    big = [x for i, c in enumerate(big) for x in (c, ToolResultBlock(tool_call_id=c.id, content=f"result {i} " + "x" * 5000))]
    _, evidence = answer_check.material([*big, TextBlock(text="done " * 20)])
    assert len(evidence) <= answer_check.MAX_EVIDENCE + 200 and "result 39" in evidence and "result 0 " not in evidence   # the newest are kept


async def test_unbacked_statements_are_named_and_backed_ones_only_counted(conn, on, monkeypatch):
    asked = judged(monkeypatch, {"claims": [
        {"claim": "Port 22 is open", "verdict": "supported", "evidence": "22/tcp open ssh"},
        {"claim": "Port 80 is open", "verdict": "contradicted", "evidence": "80/tcp closed http"},
        {"statement": "The admin panel has default credentials", "verdict": "unsupported", "evidence": ""},   # a renamed key is taken
        {"claim": "nonsense verdict", "verdict": "maybe"}, "not an object"]})
    block, detail = await answer_check.judge(conn, ITEMS)
    assert (block.checked, block.supported) == (3, 1) and block.model == "anthropic/haiku"
    assert [(c["claim"], c["verdict"], c["evidence"]) for c in block.claims] == [
        ("Port 80 is open", "contradicted", "80/tcp closed http"), ("The admin panel has default credentials", "unsupported", "")]
    assert "80/tcp closed http" in asked[0]["user"] and "default credentials" in asked[0]["user"]
    assert "untrusted" in asked[0]["system"] and '{"claims": [' in asked[0]["system"] and detail["unsupported"] == 2


async def test_nothing_is_shown_when_there_is_nothing_to_say(conn, on, monkeypatch):
    judged(monkeypatch, {"claims": []})
    block, _ = await answer_check.judge(conn, ITEMS)
    assert block is not None and (block.checked, block.claims) == (0, [])
    judged(monkeypatch, None, error="the decision model did not answer")
    block, detail = await answer_check.judge(conn, ITEMS)
    assert block is None and "did not answer" in detail["error"]
    asked = judged(monkeypatch, {"claims": []})
    assert await answer_check.judge(conn, [TextBlock(text="ok")]) == (None, {})          # too short to hold a claim
    assert asked == []


async def test_a_run_with_tools_ends_with_the_check_and_the_model_never_sees_it(conn, setup, on, monkeypatch):
    judged(monkeypatch, {"claims": [{"claim": "You are root", "verdict": "contradicted", "evidence": "uid=1000"}]})
    registry.register(fake_tool("t_echo", text="uid=1000")[0])
    p = Turns("abliteration", [ToolCall("c1", "t_echo", {})], [TextDelta("The command ran and it shows that you are root on this machine.")],
              [TextDelta("second answer, long enough to be looked at by the check")])
    use(abliteration=p)
    chat, user = await setup()
    run, msg = await go(conn, chat, user)
    assert run.status == "done" and [b.type for b in msg.blocks] == ["tool_call", "tool_result", "text", "answer_check"]
    assert msg.blocks[-1].claims[0]["verdict"] == "contradicted"
    last = run.steps[-1]
    assert (last.kind, last.name, last.status, last.detail["unsupported"]) == ("decision", "answer check", "done", 1)
    # The next turn of the chat: the model gets the conversation without the check block.
    nxt = await messages.add(conn, chat.id, msg.id, "user", [TextBlock(text="and now?")])
    await go(conn, chat, nxt)
    assert all(b.type != "answer_check" for m in p.calls[-1]["messages"] for b in m.blocks)


async def test_the_check_can_be_switched_off_and_never_fails_a_run(conn, setup, on, monkeypatch):
    asked = judged(monkeypatch, {"claims": []})
    registry.register(fake_tool("t_echo", text="uid=1000")[0])
    await settings.put(conn, "answer_check", False)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_echo", {})], [TextDelta("The command ran fine and printed the user id of this account.")]))
    run, msg = await go(conn, *await setup())
    assert asked == [] and all(b.type != "answer_check" for b in msg.blocks)
    await settings.put(conn, "answer_check", True)

    async def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(answer_check.de, "complete_json", broken)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_echo", {})], [TextDelta("The command ran fine and printed the user id of this account.")]))
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and msg.status == "complete" and all(b.type != "answer_check" for b in msg.blocks)
    assert run.steps[-1].name == "answer check" and run.steps[-1].status == "error"


async def test_a_chat_without_tools_is_not_checked(conn, setup, on, monkeypatch):
    asked = judged(monkeypatch, {"claims": []})
    use(abliteration=Turns("abliteration", [TextDelta("Cross-site scripting is when a page runs script an attacker supplied.")]))
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and asked == []
