"""One registry for native and MCP tools, so permissions and logging have a single path."""
from __future__ import annotations

import re

import jsonschema

from .base import Tool

NAME_RE = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"   # the charset every provider accepts
_tools: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    if not re.match(NAME_RE, tool.name):
        raise ValueError(f"bad tool name: {tool.name!r}")
    if tool.name in _tools:
        raise ValueError(f"tool already registered: {tool.name}")
    if not isinstance(tool.parameters, dict) or tool.parameters.get("type") != "object":
        raise ValueError(f"{tool.name}: parameters must be an object schema")
    try:
        jsonschema.Draft202012Validator.check_schema(tool.parameters)
    except jsonschema.SchemaError as e:
        raise ValueError(f"{tool.name}: invalid schema: {e.message}") from None
    _tools[tool.name] = tool


def unregister_source(source: str) -> int:
    names = [n for n, t in _tools.items() if t.source == source]
    for n in names:
        del _tools[n]
    return len(names)


def get(name: str) -> Tool | None:
    return _tools.get(name)


def all() -> list[Tool]:  # noqa: A001
    return sorted(_tools.values(), key=lambda t: t.name)


def reset() -> None:
    _tools.clear()


def validate(tool: Tool, arguments: dict) -> str | None:
    """None when the arguments fit the schema, else the first error as "<path>: <message>"."""
    errors = sorted(jsonschema.Draft202012Validator(tool.parameters).iter_errors(arguments),
                    key=lambda e: list(map(str, e.absolute_path)))
    if not errors:
        return None
    e = errors[0]
    path = "$" + "".join(f".{p}" for p in e.absolute_path)
    return f"{path}: {e.message}"
