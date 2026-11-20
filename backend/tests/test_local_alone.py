"""A local model works alone: it has no `delegate` and no `swarm`.

A subagent runs on the chat's own model. A local model that delegates therefore hands the task to itself: it reads
the task once more from an empty context, on a machine that runs one local model at a time, and nothing is won. The
model that asks is a local one in a chat set to a local provider (Ollama, colibri), and in a run that went on with the
local model after the cloud one failed at its first call."""
import asyncio
import json

import pytest

from hunterai import db, engine, modes, permissions, subagents
from hunterai.providers import registry
from hunterai.providers.base import Options, ProviderError, TextDelta, ToolCall
from hunterai.repo import settings
from hunterai.tools import registry as tools
from hunterai.tools import runner
from tests.test_engine import no_bytes, setup, use  # noqa: F401 (setup is a fixture)
from tests.test_subagents import BRIEF, go
from tests.test_swarm import Hive, Judge, task
from tests.toolkit import Turns, fake_tool

LOCAL = ("ollama", "colibri")
RULES = dict(modes.ORCHESTRATOR_RULES)
BOTH = [ToolCall("d1", "delegate", BRIEF), ToolCall("s1", "swarm", {"tasks": [task("a"), task("b")]})]


@pytest.fixture
async def world(conn, setup):
    """delegate, swarm and t_echo registered; the decision model is a cloud one that accepts every answer."""
    tools.register(subagents.tool())
    tools.register(subagents.swarm_tool())
    echo, ran = fake_tool("t_echo", text="HTTP 200")
    tools.register(echo)
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    judge = Judge("decider")
    registry.override("decider", judge)
    return type("W", (), {"ran": ran, "judge": judge})


async def subruns(conn) -> int:
    return (await (await conn.execute("select count(*) from subruns")).fetchone())[0]


def results_of(msg):
    return [b for b in msg.blocks if b.type == "tool_result"]


# ---- a chat set to a local model

@pytest.mark.parametrize("provider", LOCAL)
async def test_a_chat_on_a_local_model_is_offered_neither_delegate_nor_swarm(conn, world, setup, provider):
    chat, _ = await setup(provider=provider)
    assert [t.name for t in await runner.offered_tools(conn, chat)] == ["t_echo"]


async def test_a_chat_on_a_cloud_model_is_offered_both(conn, world, setup):
    chat, _ = await setup()
    assert sorted(t.name for t in await runner.offered_tools(conn, chat)) == ["delegate", "swarm", "t_echo"]


@pytest.mark.parametrize("provider", LOCAL)
async def test_a_cloud_chat_keeps_delegate_when_only_the_decision_model_is_local(conn, world, setup, provider):
    """What stays as it was: the chat's model does the subagent's work, the decision model only judges the answer."""
    await settings.put(conn, "decision_provider", provider)
    chat, _ = await setup()
    assert "delegate" in [t.name for t in await runner.offered_tools(conn, chat)]


@pytest.mark.parametrize("provider", LOCAL)
async def test_the_local_model_is_told_of_no_subagents_and_a_call_for_one_runs_nothing(conn, world, setup, provider):
    p = Turns(provider, BOTH, [TextDelta("I did it myself.")])
    use(**{provider: p})
    run, msg = await go(conn, *await setup(provider=provider))
    assert run.status == "done", run.error
    said = p.calls[0]
    assert [t.name for t in said["tools"]] == ["t_echo"] and "Tools available now: t_echo." in said["system"]
    assert RULES["delegate"] not in said["system"] and RULES["swarm"] not in said["system"]
    # a model that asks for one all the same (an earlier turn of the chat was a cloud model's) gets an error, and no model is called for it
    got = results_of(msg)
    assert len(got) == 2 and all(b.is_error and "Unknown tool" in b.content for b in got)
    assert len(p.calls) == 2 and world.ran == [] and world.judge.calls == 0 and await subruns(conn) == 0


async def test_the_cloud_model_is_told_of_both(conn, world, setup):
    p = Turns("abliteration", [TextDelta("hello")])
    use(abliteration=p)
    await go(conn, *await setup())
    said = p.calls[0]
    assert sorted(t.name for t in said["tools"]) == ["delegate", "swarm", "t_echo"]
    assert RULES["delegate"] in said["system"] and RULES["swarm"] in said["system"]


# ---- a run that went on with the local model

async def test_a_run_that_went_on_with_the_local_model_starts_no_subagent(conn, world, setup):
    """The tool list of a run is fixed before its first model call. When the cloud model fails at that call the run
    goes on with the local one, which was told of both tools: a call to either is refused with the reason, and neither
    the cloud model nor the local one is asked to be a subagent."""
    cloud = Turns("abliteration", ProviderError("unavailable", "down"))
    local = Turns("ollama", BOTH, [TextDelta("I did it myself.")])
    use(abliteration=cloud, ollama=local)
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and msg.provider == "ollama", run.error
    got = results_of(msg)
    assert len(got) == 2 and all(b.is_error for b in got), [b.content[:200] for b in got]
    assert all("local model" in b.content and "yourself" in b.content for b in got)
    assert all("Unknown tool" not in b.content for b in got)            # refused with the reason, not as a tool that does not exist
    assert (len(cloud.calls), len(local.calls)) == (1, 2)
    assert world.ran == [] and world.judge.calls == 0 and await subruns(conn) == 0
    assert [s.kind for s in run.steps].count("subagent") == 0


async def test_the_owner_is_not_asked_about_a_subagent_that_will_not_run(conn, world, setup):
    """With "ask" set for both tools, the refusal comes first: nothing is put to the owner."""
    await settings.put(conn, "permissions", {"tools": {"delegate": "ask", "swarm": "ask"}})
    use(abliteration=Turns("abliteration", ProviderError("unavailable", "down")),
        ollama=Turns("ollama", BOTH, [TextDelta("I did it myself.")]))
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    over = asyncio.create_task(engine.wait(run_id))
    asked = []
    while not over.done():
        for req in permissions.pending(run_id):
            asked.append(req.tool)
            permissions.answer(req.run_id, req.id, permissions.Answer("deny", ""))
        await asyncio.sleep(0.01)
    await over
    assert asked == []


async def test_a_run_on_the_cloud_model_still_delegates(conn, world, setup):
    """The guard: a run that did not fall back is not taken for one that did."""
    p = Turns("abliteration", [ToolCall("d1", "delegate", BRIEF)], [ToolCall("e1", "t_echo", {"q": 1})],
              [TextDelta("It answers.")], [TextDelta("ok")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    got = results_of(msg)
    assert run.status == "done" and len(got) == 1 and not got[0].is_error, got[0].content
    assert world.ran == [{"q": 1}] and await subruns(conn) == 1


# ---- colibri is a local provider like Ollama

async def test_a_swarm_is_refused_for_a_colibri_decision_model(conn, world, setup):
    """colibri works on one request at a time: the checks of several answers at once would wait in its queue."""
    await settings.put(conn, "decision_provider", "colibri")
    await settings.put(conn, "decision_model", "qwen36-27b")
    use(abliteration=Turns("abliteration", [ToolCall("s1", "swarm", {"tasks": [task("a"), task("b")]})], [TextDelta("ok")]))
    run, msg = await go(conn, *await setup())
    block = results_of(msg)[0]
    assert block.is_error and "cloud decision model" in block.content and "colibri/qwen36-27b" in block.content, block.content
    assert "delegate" in block.content and world.ran == [] and await subruns(conn) == 0


# ---- a cloud model the owner chose for the subagents of a local chat

async def stand_in(conn, provider="abliteration", model="cloud-1"):
    await settings.put(conn, "subagent_provider", provider)
    await settings.put(conn, "subagent_model", model)


TWICE = ([ToolCall("d1", "delegate", BRIEF)], [ToolCall("d2", "delegate", BRIEF)], [TextDelta("I did the rest myself.")])


@pytest.mark.parametrize("provider", LOCAL)
async def test_with_a_cloud_model_chosen_for_it_a_local_chat_is_offered_both_again(conn, world, setup, provider):
    use(abliteration=Turns("abliteration"))
    await stand_in(conn)
    chat, _ = await setup(provider=provider)
    assert sorted(t.name for t in await runner.offered_tools(conn, chat)) == ["delegate", "swarm", "t_echo"]


@pytest.mark.parametrize("chosen", [("abliteration", "cloud-1"), ("ollama", "qwen3:8b"), ("colibri", "m"), ("codex", "codex-default"),
                                    ("nope", "m"), ("abliteration", "")])
async def test_a_choice_that_cannot_do_the_work_leaves_the_local_chat_alone(conn, world, setup, chosen):
    """In turn: no key is set for it, a local provider (twice), a provider that has no tools here, one that
    does not exist, and a provider without a model."""
    await stand_in(conn, *chosen)
    chat, _ = await setup(provider="ollama")
    assert [t.name for t in await runner.offered_tools(conn, chat)] == ["t_echo"]


async def test_the_subagent_of_a_local_chat_works_on_the_chosen_cloud_model(conn, world, setup):
    local = Turns("ollama", [ToolCall("d1", "delegate", BRIEF)], [TextDelta("The subagent confirmed it.")])
    cloud = Turns("abliteration", [ToolCall("e1", "t_echo", {"q": 1})], [TextDelta("It answers: HTTP 200.")])
    use(ollama=local, abliteration=cloud)
    await stand_in(conn)
    run, msg = await go(conn, *await setup(provider="ollama", model="qwen3:8b", options={"context_window": 8192, "temperature": 0.2}))
    assert run.status == "done", run.error
    got = json.loads(results_of(msg)[0].content)
    assert got["status"] == "done" and got["verified"] is True and got["calls"] == 1 and world.ran == [{"q": 1}]
    assert [c["model"] for c in cloud.calls] == ["cloud-1", "cloud-1"] and all(subagents.SUBAGENT_NOTE in c["system"] for c in cloud.calls)
    assert cloud.calls[0]["options"] == Options()             # the chat's options are those of its local model, not of this one
    assert len(local.calls) == 2 and all(subagents.SUBAGENT_NOTE not in c["system"] for c in local.calls)
    names = [s.name for s in run.steps if s.kind == "model_call"]
    assert names.count("abliteration/cloud-1") == 2 and names.count("ollama/qwen3:8b") == 2


async def test_a_cloud_model_that_fails_is_not_tried_again_in_that_run_and_the_local_model_does_not_stand_in(conn, world, setup):
    local, cloud = Turns("ollama", *TWICE), Turns("abliteration", ProviderError("unavailable", "down"))
    use(ollama=local, abliteration=cloud)
    await stand_in(conn)
    run, msg = await go(conn, *await setup(provider="ollama"))
    assert run.status == "done", run.error
    first, second = results_of(msg)
    got = json.loads(first.content)
    assert got["status"] == "error" and got["verified"] is False and "down" in got["error"] and "no further subagent" in got["note"]
    assert second.is_error and "no further subagent" in second.content and "yourself" in second.content
    assert (len(cloud.calls), len(local.calls)) == (1, 3)     # the cloud model was asked once, and the local one was never the subagent


async def test_a_cloud_model_that_answers_without_doing_the_task_is_not_asked_again_in_that_run(conn, world, setup):
    local, cloud = Turns("ollama", *TWICE), Turns("abliteration", [TextDelta("I can't help with that.")])
    use(ollama=local, abliteration=cloud)
    await stand_in(conn)
    run, msg = await go(conn, *await setup(provider="ollama"))
    first, second = results_of(msg)
    got = json.loads(first.content)
    assert got["status"] == "done" and got["calls"] == 0 and got["verified"] is False and "no further subagent" in got["note"]
    assert second.is_error and "no further subagent" in second.content and len(cloud.calls) == 1


async def test_a_cloud_model_that_did_its_task_may_be_given_another(conn, world, setup):
    local = Turns("ollama", *TWICE)
    cloud = Turns("abliteration", [ToolCall("e1", "t_echo", {})], [TextDelta("done 1")], [ToolCall("e2", "t_echo", {})], [TextDelta("done 2")])
    use(ollama=local, abliteration=cloud)
    await stand_in(conn)
    run, msg = await go(conn, *await setup(provider="ollama"))
    assert [json.loads(b.content)["status"] for b in results_of(msg)] == ["done", "done"] and await subruns(conn) == 2
    assert all("no further subagent" not in json.loads(b.content)["note"] for b in results_of(msg))


async def test_a_swarm_of_a_local_chat_runs_on_the_chosen_cloud_model(conn, world, setup):
    tasks = [task("a"), task("b")]
    local = Turns("ollama", [ToolCall("s1", "swarm", {"tasks": tasks})], [TextDelta("The swarm is done.")])
    cloud = Hive("abliteration", tasks)
    use(ollama=local, abliteration=cloud)
    await stand_in(conn)
    run, msg = await go(conn, *await setup(provider="ollama"))
    assert run.status == "done", run.error
    got = json.loads(results_of(msg)[0].content)
    assert [r["status"] for r in got["results"]] == ["done", "done"] and all(r["verified"] for r in got["results"])
    assert cloud.parent_calls == 0 and sorted(c["q"] for c in world.ran) == ["a", "b"] and len(local.calls) == 2


async def test_a_run_that_went_on_with_the_local_model_may_use_the_chosen_cloud_model(conn, world, setup):
    """The cloud model of the chat failed at the first call; the one chosen for a local model's subagents is another."""
    chats_own = Turns("abliteration", ProviderError("unavailable", "down"))
    local = Turns("ollama", [ToolCall("d1", "delegate", BRIEF)], [TextDelta("ok")])
    other = Turns("openai", [ToolCall("e1", "t_echo", {})], [TextDelta("It answers.")])
    use(abliteration=chats_own, ollama=local, openai=other)
    await stand_in(conn, "openai", "gpt-x")
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and msg.provider == "ollama", run.error
    got = json.loads(results_of(msg)[0].content)
    assert got["status"] == "done" and (len(other.calls), len(chats_own.calls), len(local.calls)) == (2, 1, 2)


@pytest.mark.parametrize("name", ["codex"])
async def test_the_login_of_a_command_line_program_is_never_the_chosen_cloud_model(conn, world, setup, name):
    """Whatever such a provider says of itself: the choice is among the providers that are called with an API key."""
    use(**{name: Turns(name)})
    await stand_in(conn, name, "m")
    chat, _ = await setup(provider="ollama")
    assert name not in subagents.stand_in_providers()
    assert [t.name for t in await runner.offered_tools(conn, chat)] == ["t_echo"]
