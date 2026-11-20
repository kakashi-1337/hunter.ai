import asyncio

import pytest

from hunterai import db, engine, hub
from hunterai.modes import SYSTEM_PROMPTS, build_system
from hunterai.providers import registry
from hunterai.providers.base import (ModelInfo, Options, ProviderError, Stop, TextDelta,
                                     ThinkingDelta, Usage)
from hunterai.repo import chats, messages, runs, settings, targets
from hunterai.types import ImageBlock, TextBlock


class Scripted:
    """Provider whose successive calls follow a script of behaviours."""

    def __init__(self, name, *script):
        self.name, self.script, self.calls = name, list(script), []

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="m", label="m")]

    async def stream(self, model, system, msgs, options):
        self.calls.append({"model": model, "system": system, "messages": msgs,
                           "options": options})
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, ProviderError):
            raise step
        if step == "partial":
            yield TextDelta("half")
            raise ProviderError("unavailable", "dropped")
        if step == "boom":
            raise RuntimeError("adapter bug")
        if step == "slow":
            for i in range(5):
                await asyncio.sleep(0.05)
                yield TextDelta(f"c{i} ")
        else:
            yield ThinkingDelta("thinking")
            yield TextDelta(f"{self.name} says hi")
        yield Usage(3, 5)
        yield Stop("end")


async def no_bytes(upload_id):
    return b"IMG"


@pytest.fixture
async def setup(conn):
    async def make(provider="abliteration", model="abliterated-model", text="hi", target_id=None,
                   mode="pentesting", options=None):
        chat = await chats.create(conn, target_id, mode, provider, model, options or {})
        user = await messages.add(conn, chat.id, None, "user", [TextBlock(text=text)])
        return chat, user
    return make


async def go(conn, chat, user, load_bytes=no_bytes):
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, load_bytes)
    await engine.wait(run_id)
    return await runs.get(conn, run_id), await messages.get(conn, mid)


def use(**providers):
    for name, p in providers.items():
        registry.override(name, p)


async def test_happy_path_records_trajectory(conn, setup):
    use(abliteration=Scripted("abliteration"))
    chat, user = await setup()
    run, msg = await go(conn, chat, user)
    assert run.status == "done" and run.error is None
    assert run.totals["turns"] == 1 and run.totals["calls"] == 0
    assert (run.totals["input_tokens"], run.totals["output_tokens"]) == (3, 5)
    assert run.totals["duration_ms"] >= 0 and run.ended_at
    assert [(s.kind, s.name, s.status) for s in run.steps] == \
        [("model_call", "abliteration/abliterated-model", "done")]
    assert [b.model_dump() for b in msg.blocks] == [
        {"type": "thinking", "text": "thinking"}, {"type": "text", "text": "abliteration says hi"}]
    assert (msg.status, msg.provider, msg.model, msg.fallback_from, msg.parent_id) == \
        ("complete", "abliteration", "abliterated-model", None, user.id)
    assert [m.id for m in await messages.path(conn, chat.id)] == [user.id, msg.id]


async def test_fallback_on_unavailable_before_output(conn, setup):
    ablit = Scripted("abliteration", ProviderError("unavailable", "down", 503))
    oll = Scripted("ollama")
    use(abliteration=ablit, ollama=oll)
    chat, user = await setup()
    run, msg = await go(conn, chat, user)
    assert run.status == "done"
    assert (msg.provider, msg.model, msg.fallback_from) == \
        ("ollama", "qwen3:8b", "abliteration/abliterated-model")
    assert [(s.kind, s.status) for s in run.steps] == \
        [("model_call", "error"), ("fallback", "done"), ("model_call", "done")]
    assert run.steps[1].detail["reason"].startswith("unavailable")
    assert run.totals["turns"] == 2 and oll.calls[0]["model"] == "qwen3:8b"
    assert msg.blocks[-1].text == "ollama says hi"


async def test_fallback_model_comes_from_settings(conn, setup):
    await settings.put(conn, "fallback_model", "llama3.1:latest")
    oll = Scripted("ollama")
    use(abliteration=Scripted("abliteration", ProviderError("timeout", "slow")), ollama=oll)
    run, msg = await go(conn, *await setup())
    assert oll.calls[0]["model"] == "llama3.1:latest" and msg.model == "llama3.1:latest"


@pytest.mark.parametrize("kind", ["auth", "credits", "policy", "bad_request"])
async def test_no_fallback_on_non_eligible(conn, setup, kind):
    oll = Scripted("ollama")
    use(abliteration=Scripted("abliteration", ProviderError(kind, "nope")), ollama=oll)
    run, msg = await go(conn, *await setup())
    assert run.status == "error" and "nope" in run.error
    assert msg.status == "error" and oll.calls == []
    assert [s.kind for s in run.steps] == ["model_call"]


async def test_rate_limit_waits_then_retries_same_provider(conn, setup, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(engine, "_sleep", fake_sleep)
    ablit = Scripted("abliteration", ProviderError("rate_limit", "429", 429, retry_after=7))
    oll = Scripted("ollama")
    use(abliteration=ablit, ollama=oll)
    run, msg = await go(conn, *await setup())
    assert slept == [7] and len(ablit.calls) == 2 and oll.calls == []
    assert run.status == "done" and msg.provider == "abliteration" and msg.fallback_from is None
    assert [s.kind for s in run.steps] == ["model_call", "model_call"]


async def test_rate_limit_wait_is_capped_and_then_falls_back(conn, setup, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(engine, "_sleep", fake_sleep)
    err = lambda: ProviderError("rate_limit", "429", 429, retry_after=600)  # noqa: E731
    oll = Scripted("ollama")
    use(abliteration=Scripted("abliteration", err(), err()), ollama=oll)
    run, msg = await go(conn, *await setup())
    assert slept == [20] and msg.provider == "ollama" and run.status == "done"


async def test_no_fallback_when_chat_provider_is_not_abliteration(conn, setup):
    oll = Scripted("ollama")
    use(anthropic=Scripted("anthropic", ProviderError("unavailable", "down")), ollama=oll)
    run, msg = await go(conn, *await setup(provider="anthropic", model="claude-x"))
    assert run.status == "error" and oll.calls == []


async def test_fallback_itself_does_not_fall_back(conn, setup):
    oll = Scripted("ollama", ProviderError("unavailable", "ollama down"))
    use(abliteration=Scripted("abliteration", ProviderError("unavailable", "down")), ollama=oll)
    run, msg = await go(conn, *await setup())
    assert run.status == "error" and "ollama down" in run.error and len(oll.calls) == 1
    assert msg.status == "error"


async def test_partial_output_then_error_keeps_text(conn, setup):
    oll = Scripted("ollama")
    use(abliteration=Scripted("abliteration", "partial"), ollama=oll)
    run, msg = await go(conn, *await setup())
    assert msg.blocks[-1].text == "half" and msg.status == "error"
    assert run.status == "error" and oll.calls == []
    assert [s.kind for s in run.steps] == ["model_call"]


async def test_unexpected_adapter_exception_ends_run_as_error(conn, setup):
    use(abliteration=Scripted("abliteration", "boom"))
    run, msg = await go(conn, *await setup())
    assert run.status == "error" and "RuntimeError" in run.error and msg.status == "error"


async def test_unknown_provider_ends_run_as_error(conn, setup):
    run, msg = await go(conn, *await setup(provider="nope", model="x"))
    assert run.status == "error" and "nope" in run.error


async def test_run_survives_subscriber_disconnect(conn, setup):
    use(abliteration=Scripted("abliteration", "slow"))
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    sub = hub.subscribe(run_id)
    first = await sub.__anext__()
    assert first["type"] == "run_started"
    await sub.aclose()
    await engine.wait(run_id)
    msg = await messages.get(conn, mid)
    assert msg.status == "complete" and msg.blocks[-1].text == "c0 c1 c2 c3 c4 "


async def test_late_subscriber_gets_replay(conn, setup):
    use(abliteration=Scripted("abliteration"))
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    events = [e async for e in hub.subscribe(run_id)]
    types = [e["type"] for e in events]
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert "text_delta" in types and "thinking_delta" in types and "step_finished" in types
    assert events[0] == {"type": "run_started", "run_id": run_id, "assistant_message_id": mid}
    fin = events[-1]
    assert fin["status"] == "done" and fin["totals"]["turns"] == 1
    assert fin["message"]["id"] == mid and fin["message"]["status"] == "complete"


async def test_two_subscribers_both_get_everything(conn, setup):
    use(abliteration=Scripted("abliteration", "slow"))
    chat, user = await setup()
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)

    async def drain():
        return [e["type"] async for e in hub.subscribe(run_id)]
    a, b = await asyncio.gather(drain(), drain())
    assert a == b and a.count("text_delta") == 5


async def test_fallback_event_published(conn, setup):
    use(abliteration=Scripted("abliteration", ProviderError("unavailable", "down")),
        ollama=Scripted("ollama"))
    chat, user = await setup()
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await engine.wait(run_id)
    fb = [e async for e in hub.subscribe(run_id) if e["type"] == "fallback"]
    assert len(fb) == 1
    assert (fb[0]["from_provider"], fb[0]["from_model"], fb[0]["to_provider"],
            fb[0]["to_model"]) == ("abliteration", "abliterated-model", "ollama", "qwen3:8b")


async def test_partial_text_is_flushed_while_streaming(conn, setup, monkeypatch):
    monkeypatch.setattr(engine, "FLUSH_SECONDS", 0)
    use(abliteration=Scripted("abliteration", "slow"))
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    seen = ""
    async for e in hub.subscribe(run_id):
        if e["type"] == "text_delta" and not seen:
            await asyncio.sleep(0.02)
            m = await messages.get(conn, mid)
            seen = m.blocks[-1].text if m.blocks else ""
            assert m.status == "streaming"
    assert seen.startswith("c0")


async def test_recover_marks_interrupted_and_keeps_partial(conn, setup):
    chat, user = await setup()
    msg = await messages.add(conn, chat.id, user.id, "assistant", [TextBlock(text="partial")],
                             status="streaming")
    run = await runs.create(conn, chat.id, user.id, msg.id)
    await runs.add_step(conn, run.id, "model_call", "abliteration/x", {})
    await engine.recover(conn)
    r = await runs.get(conn, run.id)
    m = await messages.get(conn, msg.id)
    assert r.status == "interrupted" and r.ended_at and r.steps[0].status == "interrupted"
    assert m.status == "interrupted" and m.blocks[0].text == "partial"
    assert await runs.has_running(conn, chat.id) is False


async def test_cancel(conn, setup):
    use(abliteration=Scripted("abliteration", "slow"))
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    async for e in hub.subscribe(run_id):
        if e["type"] == "text_delta":
            assert await engine.cancel(run_id) is True
            break
    await engine.wait(run_id)
    r, m = await runs.get(conn, run_id), await messages.get(conn, mid)
    assert r.status == "cancelled" and m.status == "cancelled"
    assert m.blocks[-1].text.startswith("c0")
    assert r.steps[0].status == "cancelled"
    assert await engine.cancel(run_id) is False
    assert await engine.cancel("unknown") is False


async def test_title_set_from_first_user_message(conn, setup):
    use(abliteration=Scripted("abliteration"))
    chat, user = await setup(text="find idor in\nthe   billing api " + "x" * 80)
    await go(conn, chat, user)
    title = (await chats.get(conn, chat.id)).title
    assert title.startswith("find idor in the billing api") and len(title) <= 60 and "\n" not in title


async def test_title_not_overwritten_and_not_set_on_error(conn, setup):
    use(abliteration=Scripted("abliteration", ProviderError("auth", "bad key")))
    chat, user = await setup()
    await go(conn, chat, user)
    assert (await chats.get(conn, chat.id)).title == "New chat"
    await chats.update(conn, chat.id, title="Mine")
    use(abliteration=Scripted("abliteration"))
    await go(conn, chat, user)
    assert (await chats.get(conn, chat.id)).title == "Mine"


async def test_context_is_path_to_user_message_with_system_and_options(conn, setup):
    p = Scripted("abliteration")
    use(abliteration=p)
    t = await targets.create(conn, "acme", "prod only", {"in": ["*.acme.test"], "out": ["pay.acme.test"]})
    chat, u1 = await setup(target_id=t.id, options={"temperature": 0.3, "effort": "high"})
    await go(conn, chat, u1)
    a_err = await messages.add(conn, chat.id, u1.id, "assistant", [], status="error")
    u2 = await messages.add(conn, chat.id, a_err.id, "user", [TextBlock(text="again")])
    await messages.add(conn, chat.id, u2.id, "user", [TextBlock(text="LATER - must not be sent")])
    await go(conn, chat, u2)
    call = p.calls[-1]
    assert [m.role for m in call["messages"]] == ["user", "user"]
    assert call["messages"][-1].blocks[0].text == "again"
    assert "*.acme.test" in call["system"] and "pay.acme.test" in call["system"]
    assert call["options"] == Options(temperature=0.3, effort="high")


async def test_retry_context_excludes_previous_answer(conn, setup):
    p = Scripted("abliteration")
    use(abliteration=p)
    chat, user = await setup()
    _, first = await go(conn, chat, user)
    _, second = await go(conn, chat, user)
    assert [m.role for m in p.calls[-1]["messages"]] == ["user"]
    assert await messages.siblings(conn, first.id) == [first.id, second.id]


async def test_image_bytes_loaded_for_the_adapter(conn, setup):
    p = Scripted("abliteration")
    use(abliteration=p)
    chat = await chats.create(conn, None, "coding", "abliteration", "abliterated-model", {})
    user = await messages.add(conn, chat.id, None, "user",
                              [TextBlock(text="see"), ImageBlock(upload_id="up1", mime="image/png")])
    await go(conn, chat, user)
    assert p.calls[0]["messages"][0].images == {"up1": b"IMG"}


async def test_missing_image_bytes_do_not_kill_the_run(conn, setup):
    async def missing(upload_id):
        raise FileNotFoundError(upload_id)
    p = Scripted("abliteration")
    use(abliteration=p)
    chat = await chats.create(conn, None, "coding", "abliteration", "abliterated-model", {})
    user = await messages.add(conn, chat.id, None, "user",
                              [TextBlock(text="see"), ImageBlock(upload_id="gone", mime="image/png")])
    run, _ = await go(conn, chat, user, load_bytes=missing)
    assert run.status == "done" and p.calls[0]["messages"][0].images == {}


def test_system_prompts_cover_all_modes():
    assert set(SYSTEM_PROMPTS) == {"plan", "pentesting", "coding"}
    assert all(len(v) > 40 for v in SYSTEM_PROMPTS.values())


async def test_system_prompt_includes_target_scope(conn):
    t = await targets.create(conn, "acme", "note here", {"in": ["a.test", "b.test"], "out": ["c.test"]})
    s = build_system("pentesting", t)
    for needle in ("acme", "note here", "a.test", "b.test", "c.test"):
        assert needle in s
    assert build_system("pentesting", None).startswith(SYSTEM_PROMPTS["pentesting"])
    assert build_system("bogus", None) == build_system("pentesting", None)


async def test_run_listing_and_totals(conn, setup):
    use(abliteration=Scripted("abliteration"))
    chat, user = await setup()
    await go(conn, chat, user)
    await go(conn, chat, user)
    lst = await runs.list_for_chat(conn, chat.id)
    assert len(lst) == 2 and all(r.totals["turns"] == 1 for r in lst)
    assert await runs.get(conn, "missing") is None


async def test_shutdown_stops_running_runs_and_records_them(conn, setup):
    use(abliteration=Scripted("abliteration", "slow"))
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await asyncio.sleep(0.07)
    await engine.shutdown()
    assert (await runs.get(conn, run_id)).status == "cancelled"
    assert (await messages.get(conn, mid)).status == "cancelled"
    await engine.shutdown()  # nothing left: must be a no-op


async def test_cancel_before_the_run_task_has_started(conn, setup):
    use(abliteration=Scripted("abliteration", "slow"))
    chat, user = await setup()
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    assert await engine.cancel(run_id) is True
    await engine.wait(run_id)
    assert (await runs.get(conn, run_id)).status == "cancelled"
    assert (await messages.get(conn, mid)).status == "cancelled"
    types = [e["type"] async for e in hub.subscribe(run_id)]
    assert types[-1] == "run_finished"


def test_no_offered_tools_means_the_no_tools_notice():
    from hunterai.modes import build_system
    for mode in ("plan", "pentesting", "coding"):
        prompt = build_system(mode, None)
        assert "cannot run commands" in prompt
        assert "Never say you ran" in prompt


async def test_a_run_that_announced_its_end_is_waited_for_before_its_test_ends(conn, setup, monkeypatch):
    """A test may stop looking at a run once `run_finished` is published. The run is then still closing its
    database connection. If the event loop closed under it, the connection's thread would fail with "Event loop
    is closed", and a later test would be blamed for it."""
    import time

    import aiosqlite

    from tests.toolkit import runs_have_ended
    real = aiosqlite.Connection.close

    async def slow_close(self):
        if self is not conn:                         # the run's own connection, on a slow disk
            await self._execute(time.sleep, 0.3)
        return await real(self)
    use(abliteration=Scripted("abliteration"))
    chat, user = await setup()
    run_id, _ = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    monkeypatch.setattr(aiosqlite.Connection, "close", slow_close)
    assert [e["type"] async for e in hub.subscribe(run_id)][-1] == "run_finished"
    assert engine.is_active(run_id)                  # announced, and not done yet
    await runs_have_ended()                          # what every test does when it ends (conftest)
    assert not engine.is_active(run_id)
