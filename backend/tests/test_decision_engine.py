import json

import httpx
import pytest
import respx

from hunterai.decision import engine as de
from hunterai.providers import ollama, openai_compat, registry
from hunterai.providers.base import (ModelInfo, Options, ProviderError, Stop, TextDelta, Usage,
                                     WireMessage)
from hunterai.providers.base import ToolCall
from hunterai.repo import settings
from hunterai.tools import decide_tools, registry as tools
from hunterai.types import TextBlock
from tests.toolkit import run_call, seeded_run

QS = {"ok": {"type": "noul", "instructions": "Is it done?"},
      "kind": {"type": "choice", "instructions": "Which?", "criteria": {"a": "first", "b": "second"}},
      "sev": {"type": "score", "instructions": "How bad?", "criteria": ["low", "mid", "high"]}}


class Json:
    """A provider that answers with a fixed text (or raises) and records what it was sent."""

    def __init__(self, name, *replies):
        self.name, self.replies, self.calls = name, list(replies), []

    def configured(self):
        return True

    async def models(self):
        return [ModelInfo(id="m", label="m")]

    async def stream(self, model, system, messages, options):
        self.calls.append({"model": model, "system": system, "messages": messages, "options": options})
        reply = self.replies.pop(0) if self.replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        yield TextDelta(reply if isinstance(reply, str) else json.dumps(reply))
        yield Usage(11, 7)
        yield Stop("end")


def test_prompt_schema_and_wrapping_follow_the_original():
    assert de.wrap_state("a <b> c") == "<document>\na \\u003cb\\u003e c\n</document>"
    p = de.question_prompt(QS)
    assert 'Question "ok" (yes/no probability): Is it done?' in p
    assert 'Question "kind" (choose one — give a probability per option): Which?\nOptions:\n  a: first\n  b: second' in p
    assert 'Levels (low → high):\n  0: low\n  1: mid\n  2: high' in p
    s = de.answer_schema(QS)
    assert s["required"] == ["ok", "kind", "sev"] and s["additionalProperties"] is False
    assert s["properties"]["ok"] == {"type": "number", "minimum": 0, "maximum": 1}
    assert s["properties"]["sev"]["required"] == ["0", "1", "2"]
    with pytest.raises(ValueError):
        de.wrap_state("x" * 60_001)
    for bad in ({}, {"q": {"type": "nope", "instructions": "x"}}, {"q": {"type": "noul", "instructions": " "}},
                {"q": {"type": "choice", "instructions": "x", "criteria": {"only": "one"}}},
                {"q": {"type": "score", "instructions": "x", "criteria": ["one"]}}):
        with pytest.raises(ValueError):
            de.validate_questions(bad)


async def test_decide_uses_the_decision_model_and_processes_each_answer(conn):
    p = Json("abliteration", {"ok": 0.9, "kind": {"a": 0.2, "b": 0.8}, "sev": {"0": 0.1, "1": 0.2, "2": 0.7}})
    registry.override("abliteration", p)
    out = await de.decide(conn, "the doc", QS)
    assert out["answers"]["ok"] == {"probability": 0.9, "verdict": "yes"}
    assert out["answers"]["kind"]["choice"] == "b" and out["answers"]["kind"]["action"] == "review"
    assert out["answers"]["sev"]["level"] == "high" and "invalid" not in out
    assert (out["provider"], out["usage"]) == ("abliteration", {"input_tokens": 11, "output_tokens": 7})
    call = p.calls[0]
    assert call["system"] == de.SYSTEM_PROMPT and call["options"].json_schema == de.answer_schema(QS)
    assert call["options"].temperature == 0
    text = call["messages"][0].blocks[0].text
    assert text.startswith("<document>\nthe doc\n</document>") and 'Question "sev"' in text
    await settings.put(conn, "decision_provider", "ollama")
    await settings.put(conn, "decision_model", "tiny")
    o = Json("ollama", {"ok": 0.1, "kind": {"a": 1, "b": 0}, "sev": {"0": 1, "1": 0, "2": 0}})
    registry.override("ollama", o)
    out = await de.decide(conn, "d", QS)
    assert (out["provider"], out["model"], o.calls[0]["model"]) == ("ollama", "tiny", "tiny")


async def test_reply_in_a_code_fence_or_with_text_around_it_is_read(conn):
    registry.override("abliteration", Json("abliteration", 'Here you go:\n```json\n{"ok": 0.2}\n```\nDone.'))
    out = await de.decide(conn, "d", {"ok": QS["ok"]})
    assert out["answers"]["ok"] == {"probability": 0.2, "verdict": "no"}


@pytest.mark.parametrize("reply", ["not json at all", "[1, 2]", '{"ok": "high"}', "", '{"other": 1}'])
async def test_a_bad_reply_is_invalid_and_never_acts(conn, reply):
    registry.override("abliteration", Json("abliteration", reply))
    out = await de.decide(conn, "d", QS)
    assert out["invalid"] is True
    assert out["answers"]["ok"]["invalid"] is True and out["answers"]["ok"]["verdict"] == "uncertain"
    assert out["answers"]["kind"]["action"] == "abstain" and out["answers"]["sev"]["action"] == "abstain"


async def test_default_then_fallback_and_total_failure(conn):
    registry.override("abliteration", Json("abliteration", ProviderError("unavailable", "down")))
    fb = Json("ollama", {"ok": 0.8})
    registry.override("ollama", fb)
    out = await de.decide(conn, "d", {"ok": QS["ok"]})
    assert out["provider"] == "ollama" and out["fallback_from"].startswith("abliteration/")
    assert out["answers"]["ok"]["verdict"] == "yes"
    registry.override("abliteration", Json("abliteration", ProviderError("auth", "bad key")))
    out = await de.decide(conn, "d", {"ok": QS["ok"]})          # not eligible for fallback
    assert out["invalid"] is True and "bad key" in out["error"] and len(fb.calls) == 1
    registry.override("abliteration", Json("abliteration", ProviderError("timeout", "slow")))
    registry.override("ollama", Json("ollama", ProviderError("unavailable", "no ollama")))
    out = await de.decide(conn, "d", {"ok": QS["ok"]})          # nothing answers: invalid, not an exception
    assert out["invalid"] is True and out["answers"]["ok"]["invalid"] is True


@respx.mock
async def test_adapters_send_the_schema_natively(monkeypatch):
    schema = de.answer_schema({"ok": QS["ok"]})
    msgs = [WireMessage("user", [TextBlock(text="q")])]
    monkeypatch.setenv("ABLIT_API_KEY", "ak_test")
    route = respx.post("https://api.abliteration.ai/v1/chat/completions").mock(
        return_value=httpx.Response(200, text='data: {"choices":[{"delta":{"content":"{}"}}]}\n\ndata: [DONE]\n\n'))
    [e async for e in openai_compat.abliteration().stream("abliterated-model", "s", msgs, Options(json_schema=schema))]
    sent = json.loads(route.calls.last.request.content)
    assert sent["response_format"] == {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
    route = respx.post("http://127.0.0.1:11434/api/chat").mock(
        return_value=httpx.Response(200, text=json.dumps({"message": {"content": "{}"}, "done": True}) + "\n"))
    [e async for e in ollama.make().stream("qwen3:8b", "s", msgs, Options(json_schema=schema))]
    assert json.loads(route.calls.last.request.content)["format"] == schema
    assert Options(json_schema=schema).only(["temperature"]).json_schema == schema    # never filtered away


async def test_the_six_tools_run_through_the_runner(conn):
    for t in decide_tools.tools():
        tools.register(t)
    assert sorted(t.name for t in decide_tools.tools()) == ["decide_check", "decide_classify", "decide_decide",
                                                           "decide_gate", "decide_rank", "decide_score"]
    assert {t.risk for t in decide_tools.tools()} == {"read"}
    chat, run_id = await seeded_run(conn)

    async def call(name, reply, **args):
        registry.override("abliteration", Json("abliteration", *reply))
        out, _, steps = await run_call(conn, chat, run_id, ToolCall("c", name, args))
        assert not out.block.is_error, out.block.content
        return json.loads(out.block.content), steps[-1]
    got, step = await call("decide_check", [{"check": 0.85}], state="s", question="Done?")
    assert (got["probability"], got["verdict"]) == (0.85, "yes") and step.kind == "tool_call"
    assert "0.85" in step.detail["result_preview"] and step.detail["arguments"]["question"] == "Done?"
    got, _ = await call("decide_classify", [{"classify": {"x": 0.9, "y": 0.1}}], state="s", question="q",
                        options={"x": "one", "y": "two"})
    assert got["choice"] == "x" and got["action"] == "act"
    got, _ = await call("decide_score", [{"score": {"0": 0, "1": 0, "2": 1}}], state="s", question="q",
                        levels=["a", "b", "c"])
    assert got["level"] == "c"
    got, _ = await call("decide_gate", [{"risk": {"0": 0, "1": 0, "2": 0, "3": 1}, "sensitive": 0.9,
                                         "recommendation": {"allow": 0, "confirm": 0.1, "block": 0.9}}], state="rm -rf /")
    assert got["recommendation"] == "block" and got["risk_level"].startswith("High risk")
    got, _ = await call("decide_gate", ["garbage"], state="rm -rf /")
    assert got["recommendation"] == "confirm" and got["invalid"] is True          # fails closed
    got, _ = await call("decide_decide", [{"a": 0.1}], state="s", questions={"a": QS["ok"]})
    assert got["answers"]["a"]["verdict"] == "no"
    got, _ = await call("decide_rank", [{"item_1": {"0": 1, "1": 0}, "item_2": {"0": 0, "1": 1}}],
                        items=["weak", "strong"], question="How promising?", levels=["low", "high"])
    assert [r["item"] for r in got["ranked"]] == ["strong", "weak"] and got["ranked"][0]["rank"] == 1
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "decide_rank", {
        "items": ["only one"], "question": "q", "levels": ["low", "high"]}))
    assert out.block.is_error


async def test_an_empty_reply_is_asked_again_once_with_little_reasoning(conn):
    # A reasoning model can spend its whole budget thinking and return nothing.
    p = Json("abliteration", "", {"ok": 0.9})
    registry.override("abliteration", p)
    out = await de.decide(conn, "d", {"ok": QS["ok"]})
    assert out["answers"]["ok"]["verdict"] == "yes" and "invalid" not in out and len(p.calls) == 2
    o = p.calls[0]["options"]
    assert (o.effort, o.thinking, o.max_tokens) == ("low", False, de.MAX_TOKENS) and de.MAX_TOKENS >= 8192
    p = Json("abliteration", "", "")
    registry.override("abliteration", p)
    out = await de.decide(conn, "d", {"ok": QS["ok"]})
    assert out["invalid"] is True and len(p.calls) == 2          # one retry, not a loop


# ---- review fixes

async def test_a_decision_provider_without_a_model_is_not_mixed_with_the_chat_default(conn):
    await settings.put(conn, "default_provider", "abliteration")
    await settings.put(conn, "default_model", "big")
    await settings.put(conn, "decision_provider", "ollama")
    await settings.put(conn, "decision_model", "")
    assert await de.decision_model(conn) == ("abliteration", "big")
    await settings.put(conn, "decision_model", "qwen3:8b")
    assert await de.decision_model(conn) == ("ollama", "qwen3:8b")


async def test_any_failure_of_the_fallback_is_an_error_result_not_an_exception(conn, monkeypatch):
    from hunterai.providers.base import ProviderError

    async def ask(provider, model, user, schema, system=None):
        if provider == "abliteration":
            raise ProviderError("unavailable", "down")
        raise RuntimeError("adapter bug")
    monkeypatch.setattr(de, "_ask", ask)
    raw, meta = await de.complete_json(conn, "s", "u", {})
    assert raw is None and "adapter bug" in meta["error"] and meta["unreachable"] is True
