import asyncio
import json
import re

from hunterai import db, engine, hub, modes, permissions
from hunterai.providers import base
from hunterai.providers.base import ProviderError, TextDelta, ThinkingDelta, ThinkingSeal, ToolCall
from hunterai.repo import chats, messages, runs, settings
from hunterai.tools import registry
from hunterai.tools.base import Prepared, Tool, ToolResult
from hunterai.types import TextBlock
from tests.test_engine import Scripted, go, no_bytes, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, answer_next, call, fake_tool, result

KEY = "ak_secret_value_123"


async def start(conn, chat, user):
    return await engine.start_run(db.connect, chat.id, user.id, no_bytes)


async def finished(conn, run_id, mid):
    await engine.wait(run_id)
    return await runs.get(conn, run_id), await messages.get(conn, mid)


async def until_pending(run_id, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if permissions.pending(run_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no permission request was asked")


async def test_tool_round_trip_records_real_turns_and_calls(conn, setup):
    tool, ran = fake_tool("t_echo", text="uid=1000"); registry.register(tool)
    p = Turns("abliteration", [ThinkingDelta("plan"), ThinkingSeal({"provider": "x"}), TextDelta("Checking."),
                               ToolCall("c1", "t_echo", {"q": 1})], [TextDelta("You are uid 1000.")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and run.totals["turns"] == 2 and run.totals["calls"] == 1
    assert (run.totals["input_tokens"], run.totals["output_tokens"]) == (6, 10)
    assert [(s.kind, s.status) for s in run.steps] == [("model_call", "done"), ("permission", "done"), ("tool_call", "done"), ("model_call", "done")]
    assert [b.type for b in msg.blocks] == ["thinking", "text", "tool_call", "tool_result", "text"]
    assert msg.blocks[0].opaque == {"provider": "x"} and msg.blocks[3].content == "uid=1000" and msg.status == "complete"
    assert [t.name for t in p.calls[0]["tools"]] == ["t_echo"] and "Tools available now: t_echo." in p.calls[0]["system"]
    last = p.calls[1]["messages"][-1]
    assert last.role == "assistant" and [b.type for b in last.blocks] == ["thinking", "text", "tool_call", "tool_result"]
    assert (await messages.get(conn, msg.id)).blocks == msg.blocks              # survives a reload

async def test_invalid_calls_go_back_as_errors_and_three_in_a_row_end_the_run(conn, setup):     # Review Focus 3
    tool, ran = fake_tool("t_echo", schema={"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": False})
    registry.register(tool)
    p = Turns("abliteration",
              [ToolCall("c1", "nope", {})],
              [ToolCall("c2", "t_echo", {}, error="arguments are not valid JSON: x")],
              [ToolCall("c3", "t_echo", {"text": "ok"})],                         # a valid call resets the count
              [ToolCall("c4", "t_echo", {"wrong": 1})], [ToolCall("c5", "nope", {})], [ToolCall("c6", "nope", {})],
              [TextDelta("never reached")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert run.status == "error" and run.error == "The model sent 3 invalid tool calls in a row."
    assert len(p.calls) == 6 and ran == [{"text": "ok"}]
    results = [b for b in msg.blocks if b.type == "tool_result"]
    assert [r.is_error for r in results] == [True, True, False, True, True, True]
    assert 'Unknown tool "nope"' in results[0].content and "not valid JSON" in results[1].content
    assert p.calls[1]["messages"][-1].blocks[-1] == results[0]                   # the model saw the error and could retry

async def test_max_turns_stops_the_run_and_says_so(conn, setup):
    tool, ran = fake_tool("t_echo"); registry.register(tool)
    await settings.put(conn, "max_turns", 3)
    p = Turns("abliteration", *[[ToolCall(f"c{i}", "t_echo", {})] for i in range(1, 6)])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert run.status == "error" and run.error == "Stopped after 3 model turns (the max_turns setting). Send a message to continue."
    assert len(p.calls) == 3 and len(ran) == 2 and (run.totals["turns"], run.totals["calls"]) == (3, 2)
    last = msg.blocks[-1]
    assert (last.tool_call_id, last.is_error, last.content) == ("c3", True, "Not run: this run reached its limit of 3 model turns.")
    assert msg.status == "error" and [b.type for b in msg.blocks].count("tool_call") == 3

async def test_no_fallback_after_a_tool_ran(conn, setup, monkeypatch):
    async def no_sleep(s):
        pass
    monkeypatch.setattr(engine, "_sleep", no_sleep)
    tool, ran = fake_tool("t_echo"); registry.register(tool)
    ab = Turns("abliteration", [TextDelta("Checking."), ToolCall("c1", "t_echo", {})], ProviderError("unavailable", "down"),
               ProviderError("unavailable", "down"))                    # tried once more, then given up on
    ol = Turns("ollama", [TextDelta("never")])
    use(abliteration=ab, ollama=ol)
    run, msg = await go(conn, *await setup())
    assert run.status == "error" and ol.calls == [] and ran == [{}] and len(ab.calls) == 3
    assert all(s.kind != "fallback" for s in run.steps) and msg.provider == "abliteration" and msg.fallback_from is None
    assert [b.type for b in msg.blocks] == ["text", "tool_call", "tool_result"]          # every block kept

async def test_key_value_in_arguments_is_denied_and_never_stored(conn, setup, monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", KEY)
    tool, ran = fake_tool("t_echo"); registry.register(tool)
    p = Turns("abliteration", [ToolCall("c1", "t_echo", {"note": f"key is {KEY}"})], [TextDelta("I will not send that.")])
    use(abliteration=p)
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    events = [e async for e in hub.subscribe(run_id)]
    run, msg = await runs.get(conn, run_id), await messages.get(conn, mid)
    raw = (await (await conn.execute("select blocks_json from messages where id=?", (mid,))).fetchone())[0]
    assert ran == [] and run.status == "done" and msg.blocks[0].arguments == {"note": "key is [redacted]"}
    assert "permission policy (secret)" in msg.blocks[1].content
    assert [e["arguments"] for e in events if e["type"] == "tool_call"] == [{"note": "key is [redacted]"}]
    assert p.calls[1]["messages"][-1].blocks[0].arguments == {"note": "key is [redacted]"}
    assert KEY not in raw + json.dumps(events) + json.dumps([s.model_dump() for s in run.steps])

async def test_unsafe_and_repeated_call_ids_are_replaced(conn, setup):                  # Ruling 25
    tool, ran = fake_tool("t_echo"); registry.register(tool)
    p = Turns("abliteration", [ToolCall("../../evil", "t_echo", {}), ToolCall("call_0", "t_echo", {})],
              [ToolCall("call_0", "t_echo", {})], [TextDelta("done")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    ids = [b.id for b in msg.blocks if b.type == "tool_call"]
    assert len(set(ids)) == 3 and ids[1] == "call_0" and all(re.fullmatch(base.ID_RE, i) for i in ids) and len(ran) == 3
    assert sorted(b.tool_call_id for b in msg.blocks if b.type == "tool_result") == sorted(ids)
    assert [s.detail["tool_call_id"] for s in run.steps if s.kind == "tool_call"] == ids

async def test_only_twenty_calls_of_one_turn_are_run(conn, setup):
    tool, ran = fake_tool("t_echo"); registry.register(tool)
    use(abliteration=Turns("abliteration", [ToolCall(f"c{i}", "t_echo", {}) for i in range(23)], [TextDelta("ok")]))
    run, msg = await go(conn, *await setup())
    results = [b for b in msg.blocks if b.type == "tool_result"]
    assert run.status == "done" and len(ran) == 20 and len(results) == 23 and run.totals["calls"] == 20
    assert all(r.is_error and r.content.startswith("Not run: more than 20 tool calls in one turn.") for r in results[20:])

async def test_several_calls_in_one_turn_run_in_order(conn, setup):
    order = []

    def tool(name):
        async def h(ctx, args):
            order.append(name)
            return ToolResult(name)
        return Tool(name, "d", {"type": "object"}, "read", h)
    registry.register(tool("t_a"))
    registry.register(tool("t_b"))
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_b", {}), ToolCall("c2", "t_a", {})],
                           [TextDelta("ok")]))
    run, msg = await go(conn, *await setup())
    assert order == ["t_b", "t_a"] and run.status == "done"
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_call", "tool_result", "tool_result", "text"]
    assert [b.tool_call_id for b in msg.blocks[2:4]] == ["c1", "c2"]


async def test_cancel_mid_tool_closes_step_and_keeps_blocks(conn, setup):
    async def slow(ctx, args):
        await asyncio.sleep(5)
        return ToolResult("late")
    registry.register(Tool("t_slow", "d", {"type": "object"}, "read", slow))
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_slow", {})], [TextDelta("never")]))
    run_id, mid = await start(conn, *await setup())
    async for e in hub.subscribe(run_id):
        if e["type"] == "tool_started":
            assert await engine.cancel(run_id) is True
            break
    run, msg = await finished(conn, run_id, mid)
    assert run.status == "cancelled" and msg.status == "cancelled"
    assert [(s.kind, s.status) for s in run.steps][-1] == ("tool_call", "cancelled")
    assert [b.type for b in msg.blocks] == ["tool_call"]


async def test_cancel_while_waiting_for_an_answer(conn, setup):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_exec", {"command": "id"})]))
    run_id, mid = await start(conn, *await setup())
    await until_pending(run_id)
    assert await engine.cancel(run_id) is True
    run, msg = await finished(conn, run_id, mid)
    assert run.status == "cancelled" and ran == [] and permissions.pending() == []
    assert [(s.kind, s.status) for s in run.steps][-1] == ("permission", "cancelled")


async def test_denied_call_goes_back_to_the_model(conn, setup):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)
    p = Turns("abliteration", [ToolCall("c1", "t_exec", {"command": "id"})],
              [TextDelta("The owner denied it.")])
    use(abliteration=p)
    run_id, mid = await start(conn, *await setup())
    await answer_next(run_id, "deny")
    run, msg = await finished(conn, run_id, mid)
    assert run.status == "done" and ran == [] and len(p.calls) == 2
    last = p.calls[1]["messages"][-1].blocks[-1]
    assert last.type == "tool_result" and last.is_error and last.content == "The owner denied this call."


async def test_fallback_on_first_call_then_tools_continue_on_ollama(conn, setup):
    tool, ran = fake_tool("t_echo")
    registry.register(tool)
    ab = Turns("abliteration", ProviderError("unavailable", "down"))
    ol = Turns("ollama", [ToolCall("c1", "t_echo", {})], [TextDelta("ok")])
    use(abliteration=ab, ollama=ol)
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and ran == [{}] and len(ab.calls) == 1 and len(ol.calls) == 2
    assert msg.provider == "ollama" and msg.fallback_from == "abliteration/abliterated-model"
    names = [s.name for s in run.steps if s.kind == "model_call"]
    assert names[0].startswith("abliteration/") and all(n.startswith("ollama/") for n in names[1:])
    assert [t.name for t in ol.calls[0]["tools"]] == ["t_echo"]


async def test_rate_limit_retry_on_a_later_turn(conn, setup, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(engine, "_sleep", fake_sleep)
    tool, ran = fake_tool("t_echo")
    registry.register(tool)
    p = Turns("abliteration", [ToolCall("c1", "t_echo", {})],
              ProviderError("rate_limit", "slow down", 429, 2), [TextDelta("ok")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert slept == [2] and run.status == "done" and len(p.calls) == 3
    assert msg.blocks[-1].text == "ok" and all(s.kind != "fallback" for s in run.steps)


async def test_a_later_turn_that_cannot_reach_the_provider_is_tried_once_more(conn, setup, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(engine, "_sleep", fake_sleep)
    registry.register(fake_tool("t_echo")[0])
    for kind in ("timeout", "unavailable"):          # nothing was produced: the tools' work is not thrown away for one hiccup
        p = Turns("abliteration", [ToolCall("c1", "t_echo", {})], ProviderError(kind, "abliteration timed out"), [TextDelta("ok")])
        use(abliteration=p)
        run, msg = await go(conn, *await setup())
        assert run.status == "done" and len(p.calls) == 3 and msg.blocks[-1].text == "ok"
        assert all(s.kind != "fallback" for s in run.steps)
    assert slept == [engine.RETRY_PAUSE, engine.RETRY_PAUSE]
    # Twice in a row is an error, and so is any other kind of failure, at once.
    p = Turns("abliteration", [ToolCall("c1", "t_echo", {})], ProviderError("timeout", "abliteration timed out"),
              ProviderError("timeout", "abliteration timed out"))
    use(abliteration=p)
    run, _ = await go(conn, *await setup())
    assert run.status == "error" and "timed out" in run.error and len(p.calls) == 3
    p = Turns("abliteration", [ToolCall("c1", "t_echo", {})], ProviderError("auth", "bad key", 401))
    use(abliteration=p)
    run, _ = await go(conn, *await setup())
    assert run.status == "error" and len(p.calls) == 2


async def test_provider_without_tool_support_gets_the_phase1_call(conn, setup):
    tool, ran = fake_tool("t_echo")
    registry.register(tool)
    p = Scripted("abliteration")
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and modes.NO_TOOL_ACCESS in p.calls[0]["system"]


async def test_plan_mode_is_not_offered_write_or_execute(conn, setup):
    for name, risk in (("t_read", "read"), ("t_net", "network"), ("t_write", "write"),
                       ("t_exec", "execute")):
        registry.register(fake_tool(name, risk=risk)[0])
    p = Turns("abliteration", [TextDelta("a plan")])
    use(abliteration=p)
    run, _ = await go(conn, *await setup(mode="plan"))
    assert run.status == "done" and [t.name for t in p.calls[0]["tools"]] == ["t_net", "t_read"]
    assert "Tools available now: t_net, t_read." in p.calls[0]["system"]


async def test_policy_change_during_a_run_reaches_the_next_call(conn, setup):
    ran = []
    chat, user = await setup()

    async def prepare(ctx, args):
        return Prepared(grant_target=args["command"])

    async def handler(ctx, args):
        ran.append(args)
        await chats.update(conn, chat.id, mode="plan")      # the owner switches mode mid-run
        return ToolResult("ok")
    registry.register(Tool("t_exec", "d", {"type": "object"}, "execute", handler, prepare=prepare))
    p = Turns("abliteration", [ToolCall("c1", "t_exec", {"command": "id"})],
              [ToolCall("c2", "t_exec", {"command": "id"})], [TextDelta("done")])
    use(abliteration=p)
    run_id, mid = await start(conn, chat, user)
    await answer_next(run_id, "allow_once")
    run, msg = await finished(conn, run_id, mid)
    results = [b for b in msg.blocks if b.type == "tool_result"]
    assert run.status == "done" and len(ran) == 1 and "permission policy (mode)" in results[1].content
    assert p.calls[1]["tools"] == p.calls[0]["tools"] and p.calls[1]["system"] == p.calls[0]["system"]


async def test_tool_only_assistant_message_stays_in_context(conn, setup):
    chat, user = await setup()
    earlier = await messages.add(conn, chat.id, user.id, "assistant",
                                 [call("c0", "read", path="/x"), result("c0", "data")])
    empty = await messages.add(conn, chat.id, earlier.id, "user", [TextBlock(text="and?")])
    blank = await messages.add(conn, chat.id, empty.id, "assistant", [])
    nxt = await messages.add(conn, chat.id, blank.id, "user", [TextBlock(text="go on")])
    p = Turns("abliteration", [TextDelta("ok")])
    use(abliteration=p)
    await go(conn, chat, nxt)
    sent = p.calls[0]["messages"]
    assert [m.role for m in sent] == ["user", "assistant", "user", "user"]
    assert [b.type for b in sent[1].blocks] == ["tool_call", "tool_result"]


async def test_shutdown_while_parked_on_an_ask_ends_interrupted(conn, setup):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_exec", {"command": "id"})]))
    run_id, mid = await start(conn, *await setup())
    await until_pending(run_id)
    await engine.shutdown()
    run, msg = await runs.get(conn, run_id), await messages.get(conn, mid)
    assert run.status == "interrupted" and msg.status == "interrupted" and ran == []
    assert run.error == "The app stopped while this run was waiting for a permission answer."
    assert [(s.kind, s.status) for s in run.steps][-1] == ("permission", "interrupted")
    assert [b.type for b in msg.blocks] == ["tool_call"]


async def test_events_carry_the_tool_flow(conn, setup):
    tool, ran = fake_tool("t_exec", risk="execute", grant_key="command")
    registry.register(tool)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "t_exec", {"command": "id"})],
                           [TextDelta("ok")]))
    run_id, mid = await start(conn, *await setup())
    await answer_next(run_id, "allow_once")
    await engine.wait(run_id)
    types = [e["type"] for e in [e async for e in hub.subscribe(run_id)]]
    flow = ["tool_call", "permission_request", "permission_resolved", "tool_started",
            "tool_finished", "run_finished"]
    assert [t for t in types if t in flow] == flow
    assert types[0] == "run_started" and types[-1] == "run_finished"
