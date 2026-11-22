"""Before a model is loaded onto this machine, a hot processor or graphics card is given time to cool. A model
that is in memory already is called at once: the heat is its own, and the call loads nothing."""
import asyncio

import pytest

from hunterai import cooling, db, engine, hub, subagents, thermal
from hunterai.providers import colibri as colibri_server
from hunterai.providers import ollama as ollama_server
from hunterai.providers.base import Options, ProviderError, TextDelta, ToolCall
from hunterai.repo import runs
from hunterai.tools import registry
from tests.test_engine import (  # noqa: F401 (setup is a fixture)
    Scripted,
    go,
    no_bytes,
    setup,
    use,
)
from tests.toolkit import Turns, fake_tool, seeded_run

LOCAL = {"provider": "ollama", "model": "qwen3:8b"}


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    for name in ("HUNTERAI_MAX_CPU_TEMP", "HUNTERAI_MAX_GPU_TEMP", "OLLAMA_HOST", "HUNTERAI_COLIBRI_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cooling, "STEP", 0)
    in_memory(monkeypatch, False)           # unless a test says otherwise, a call has to load its model


def in_memory(monkeypatch, *answers):
    """Ollama says these of a model being in memory, one per question; the last one from then on. Returns the
    models it was asked about."""
    left, asked = list(answers), []

    async def say(model):
        asked.append(model)
        return left.pop(0) if len(left) > 1 else left[0]
    monkeypatch.setattr(ollama_server, "in_memory", say)
    return asked


def processor(monkeypatch, *readings):
    """The processor says these, one per look; the last one from then on."""
    left = list(readings)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: left.pop(0) if len(left) > 1 else left[0])
    return left


def kinds(run):
    return [s.kind for s in run.steps if s.kind in ("cooling", "model_call", "fallback", "tool_call")]


async def test_a_model_that_has_to_be_loaded_waits_for_a_hot_machine_and_is_then_called(conn, setup, monkeypatch):
    p = Scripted("ollama"); use(ollama=p)
    left = processor(monkeypatch, 93.0, 88.0, 85.0, 60.0)
    asked = in_memory(monkeypatch, False)
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "done" and len(p.calls) == 1 and kinds(run) == ["cooling", "model_call"]
    assert asked == ["qwen3:8b"]                                         # asked once, not at every look
    assert left == [60.0]                                                # under the limit of 90 was not enough: five degrees under
    step = next(s for s in run.steps if s.kind == "cooling")
    assert step.status == "done" and step.ended_at and step.name == "processor at 93 °C, limit 90 °C"
    assert step.detail == {"part": "processor", "degrees": 93.0, "limit": 90.0}
    # The page hears of the wait as it begins and as it ends, before the call.
    said = [(e["type"], e["step"]["kind"], e["step"]["status"]) async for e in hub.subscribe(run.id) if e["type"].startswith("step_")]
    assert said == [("step_started", "cooling", "running"), ("step_finished", "cooling", "done"),
                    ("step_started", "model_call", "running"), ("step_finished", "model_call", "done")]


async def test_a_run_stops_and_says_why_when_the_machine_does_not_cool(conn, setup, monkeypatch):
    p = Scripted("ollama"); use(ollama=p)
    monkeypatch.setattr(cooling, "WAIT", 0.02); monkeypatch.setattr(cooling, "STEP", 0.001)
    processor(monkeypatch, 97.0)
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "error" and p.calls == [] and kinds(run) == ["cooling"]
    assert run.error.startswith("Stopped: the processor is at 97 °C and did not cool to 85 °C")      # said as it is, not as a fault
    for word in ("its limit is 90 °C", "qwen3:8b was not called", "not in memory as far as can be told", "HUNTERAI_MAX_CPU_TEMP"):
        assert word in run.error, word
    assert [s.status for s in run.steps if s.kind == "cooling"] == ["error"]


async def test_a_hot_graphics_card_holds_the_call_up_and_names_its_own_limit(conn, setup, monkeypatch):
    p = Scripted("ollama"); use(ollama=p)
    monkeypatch.setattr(cooling, "WAIT", 0.02); monkeypatch.setattr(cooling, "STEP", 0.001)

    async def card():
        return 91.0
    monkeypatch.setattr(thermal, "gpu_temp", card)
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "error" and p.calls == []
    assert "graphics card" in run.error and "HUNTERAI_MAX_GPU_TEMP" in run.error and "HUNTERAI_MAX_CPU_TEMP" not in run.error


async def test_a_model_that_is_in_memory_is_called_at_once_on_a_hot_machine(conn, setup, monkeypatch):
    # The owner's laptop is at 96 to 98 degrees while its local model answers. That heat is the model's own: a
    # wait before each of its calls cools nothing for long and only makes the run slow.
    tool, ran = fake_tool("t_echo", text="ok")
    registry.register(tool)
    p = Turns("ollama", [ToolCall("c1", "t_echo", {})], [TextDelta("done")]); use(ollama=p)
    processor(monkeypatch, 97.0)
    asked = in_memory(monkeypatch, True)
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "done" and len(p.calls) == 2 and len(ran) == 1
    assert kinds(run) == ["model_call", "tool_call", "model_call"]
    assert asked == ["qwen3:8b", "qwen3:8b"]


async def test_a_hot_graphics_card_does_not_hold_up_a_model_in_memory_either(conn, setup, monkeypatch):
    p = Scripted("ollama"); use(ollama=p)

    async def card():
        return 91.0
    monkeypatch.setattr(thermal, "gpu_temp", card)
    in_memory(monkeypatch, True)
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "done" and kinds(run) == ["model_call"]


async def test_a_cool_machine_is_not_asked_what_it_holds(conn, setup, monkeypatch):
    p = Scripted("ollama"); use(ollama=p)
    processor(monkeypatch, 70.0)
    asked = in_memory(monkeypatch, False)
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "done" and kinds(run) == ["model_call"] and asked == []


async def test_when_ollama_does_not_say_what_it_holds_the_call_waits(conn, setup, monkeypatch):
    p = Scripted("ollama"); use(ollama=p)
    processor(monkeypatch, 93.0, 84.0)
    in_memory(monkeypatch, None)
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "done" and kinds(run) == ["cooling", "model_call"]


async def test_a_model_that_runs_elsewhere_is_not_held_up(conn, setup, monkeypatch):
    p = Scripted("abliteration"); use(abliteration=p)
    processor(monkeypatch, 97.0)
    run, _ = await go(conn, *await setup())
    assert run.status == "done" and kinds(run) == ["model_call"]


@pytest.mark.parametrize("env, provider, model, want", [
    ({}, "ollama", "qwen3:8b", True),
    ({"OLLAMA_HOST": "localhost:11434"}, "ollama", "qwen3:8b", True),
    ({"OLLAMA_HOST": "http://0.0.0.0:11434"}, "ollama", "qwen3:8b", True),
    ({"OLLAMA_HOST": "http://[::1]:11434"}, "ollama", "qwen3:8b", True),
    ({"OLLAMA_HOST": "http://192.168.1.20:11434"}, "ollama", "qwen3:8b", False),        # another machine's heat is not this one's
    ({"OLLAMA_HOST": "https://ollama.lab.example"}, "ollama", "qwen3:8b", False),
    ({}, "ollama", "glm-4.6:cloud", False),                                             # Ollama runs these in its own cloud
    ({}, "ollama", "qwen3-coder:480b-cloud", False),
    ({}, "ollama", "hf.co/someone/cloudy-9b-gguf:q6_k", True),
    ({}, "colibri", "qwen3.6-27b", False),                                              # no address: no colibri here
    ({"HUNTERAI_COLIBRI_URL": "http://127.0.0.1:8000"}, "colibri", "qwen3.6-27b", True),
    ({"HUNTERAI_COLIBRI_URL": "http://10.0.0.7:8000"}, "colibri", "qwen3.6-27b", False),
    ({}, "abliteration", "m", False),
    ({}, "anthropic", "claude", False),
])
def test_which_calls_heat_this_machine(monkeypatch, env, provider, model, want):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert cooling.here(provider, model) is want


async def test_the_local_model_a_chat_falls_back_to_waits_too(conn, setup, monkeypatch):
    local = Scripted("ollama")
    use(abliteration=Scripted("abliteration", ProviderError("unavailable", "down")), ollama=local)
    processor(monkeypatch, 95.0, 84.0)
    run, _ = await go(conn, *await setup())
    assert run.status == "done" and len(local.calls) == 1
    assert kinds(run) == ["model_call", "fallback", "cooling", "model_call"]


async def test_every_model_call_of_a_run_is_looked_at(conn, setup, monkeypatch):
    # A model can fall out of memory in the middle of a run: a tool that ran for longer than Ollama keeps it.
    tool, ran = fake_tool("t_echo", text="ok")
    registry.register(tool)
    p = Turns("ollama", [ToolCall("c1", "t_echo", {})], [TextDelta("done")]); use(ollama=p)
    processor(monkeypatch, 70.0, 94.0, 84.0)                              # cool before the first call, hot before the second
    run, _ = await go(conn, *await setup(**LOCAL))
    assert run.status == "done" and len(ran) == 1 and len(p.calls) == 2
    assert kinds(run) == ["model_call", "tool_call", "cooling", "model_call"]


async def test_the_model_of_a_colibri_server_is_in_its_memory_while_the_server_serves_it(conn, setup, monkeypatch):
    monkeypatch.setenv("HUNTERAI_COLIBRI_URL", "http://127.0.0.1:8000")
    monkeypatch.setattr(cooling, "WAIT", 0.02); monkeypatch.setattr(cooling, "STEP", 0.001)
    p = Scripted("colibri"); use(colibri=p)
    says = [colibri_server.Served("ok", "http://127.0.0.1:8000")]

    async def served(fresh=False):
        return says[0]
    p.served = served
    processor(monkeypatch, 97.0)
    asked = in_memory(monkeypatch, False)
    run, _ = await go(conn, *await setup(provider="colibri", model="qwen3.6-colibri"))
    assert run.status == "done" and kinds(run) == ["model_call"] and asked == []      # Ollama knows nothing of it
    says[0] = colibri_server.Served("down", "http://127.0.0.1:8000")                 # nobody says what is in memory there
    run, _ = await go(conn, *await setup(provider="colibri", model="qwen3.6-colibri"))
    assert run.status == "error" and kinds(run) == ["cooling"] and "qwen3.6-colibri was not called" in run.error


async def test_a_wait_for_the_machine_ends_with_the_run(conn, setup, monkeypatch):
    p = Scripted("ollama"); use(ollama=p)
    monkeypatch.setattr(cooling, "STEP", 0.01)
    processor(monkeypatch, 97.0)
    chat, user = await setup(**LOCAL)
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    for _ in range(250):
        if any(s.kind == "cooling" for s in (await runs.get(conn, run_id)).steps):
            break
        await asyncio.sleep(0.02)
    assert await engine.cancel(run_id)
    await engine.wait(run_id)
    run = await runs.get(conn, run_id)
    assert run.status == "cancelled" and p.calls == []
    assert [s.status for s in run.steps if s.kind == "cooling"] == ["cancelled"]


async def test_a_subagent_on_a_local_model_waits_too(conn, monkeypatch):
    _, run_id = await seeded_run(conn, provider="ollama")
    parent = await runs.add_step(conn, run_id, "subagent", "subagent 1 of 1", {})
    p = Turns("ollama", [TextDelta("found it")], [TextDelta("and more")]); use(ollama=p)
    processor(monkeypatch, 93.0, 84.0)

    async def call():
        return await subagents._model_call(conn, run_id, parent.id, "ollama", "qwen3:8b", "be brief", [], Options(), [], [], set())
    assert await call() == []
    steps = (await runs.get(conn, run_id)).steps
    assert [(s.kind, s.status, s.parent_step_id) for s in steps[1:]] == [("cooling", "done", parent.id), ("model_call", "done", parent.id)]
    # A machine that stays hot: the call fails with the reason, as one to a model that cannot be reached does.
    monkeypatch.setattr(cooling, "WAIT", 0.02); monkeypatch.setattr(cooling, "STEP", 0.001)
    processor(monkeypatch, 97.0)
    with pytest.raises(ProviderError) as e:
        await call()
    assert "processor" in e.value.message and "97 °C" in e.value.message and len(p.calls) == 1
    in_memory(monkeypatch, True)                                          # and no wait for a model that is in memory
    assert await call() == [] and len(p.calls) == 2
    assert [s.kind for s in (await runs.get(conn, run_id)).steps[1:]] == ["cooling", "model_call", "cooling", "model_call"]
