import dataclasses

import pytest

from hunterai import secrets
from hunterai.tools import registry
from hunterai.tools.base import Tool, ToolResult


def tool(name="echo", risk="read", **kw):
    async def h(ctx, args):
        return ToolResult(args.get("text", ""))
    return Tool(name, "d", {"type": "object", "properties": {"text": {"type": "string"}},
                            "required": ["text"], "additionalProperties": False}, risk, h, **kw)


def test_register_get_all_sorted():
    registry.register(tool("b"))
    registry.register(tool("a", source="mcp:x"))
    assert [t.name for t in registry.all()] == ["a", "b"] and registry.get("nope") is None
    assert registry.unregister_source("mcp:x") == 1 and [t.name for t in registry.all()] == ["b"]


@pytest.mark.parametrize("name", ["", "1x", "has space", "a" * 65, "dot.name"])
def test_bad_names_rejected(name):
    with pytest.raises(ValueError):
        registry.register(tool(name))


def test_duplicate_and_bad_schema_rejected():
    registry.register(tool())
    with pytest.raises(ValueError):
        registry.register(tool())
    with pytest.raises(ValueError):
        registry.register(dataclasses.replace(tool("x"), parameters={"type": "string"}))
    with pytest.raises(ValueError):
        registry.register(dataclasses.replace(
            tool("y"), parameters={"type": "object", "properties": {"a": {"type": "nope"}}}))


def test_validate_reports_first_error():
    t = tool()
    assert registry.validate(t, {"text": "hi"}) is None
    assert registry.validate(t, {}).startswith("$: ") and "text" in registry.validate(t, {})
    assert registry.validate(t, {"text": 5}).startswith("$.text: ")
    assert "extra" in registry.validate(t, {"text": "a", "extra": 1})


def test_scrub_hides_only_hunterai_keys(monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "ak_secret_value_123")
    assert secrets.scrub("x ak_secret_value_123 y") == "x [redacted] y"
    assert secrets.scrub_obj({"a": ["ak_secret_value_123"]}) == {"a": ["[redacted]"]}
    assert secrets.scrub("nothing here") == "nothing here"
    monkeypatch.setenv("OPENAI_API_KEY", "short")
    assert secrets.scrub("a short word") == "a short word"
