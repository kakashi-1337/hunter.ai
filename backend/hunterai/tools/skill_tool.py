"""`skill`: the model's way to a skill the owner installed. It only ever returns text that is already
in the database, so it is a read; what the text may then lead to is decided call by call as always."""
from __future__ import annotations

from .. import db, skills
from ..repo import skills as store
from .base import Tool, ToolContext, ToolResult


async def _skill(ctx: ToolContext, args: dict) -> ToolResult:
    conn = await db.connect()
    try:
        skill = await store.by_name(conn, str(args["name"]).strip().lstrip("/"))
        if skill is None or not skill.enabled:
            names = ", ".join(s.name for s in await store.list(conn) if s.enabled) or "none"
            return ToolResult(f"There is no skill named {args['name']!r} to load. Installed: {names}.", is_error=True)
    finally:
        await conn.close()
    return ToolResult(f'Skill "{skill.name}", installed by the owner. These are the owner\'s instructions for this '
                      f"work; follow them:\n\n{skills.for_model(skill, tools=True)}")


def tools() -> list[Tool]:
    return [Tool("skill", "Load a skill the owner installed: a set of instructions for a kind of work. Give its name "
                 "from the list of skills in your instructions, before you start the work it is for.",
                 {"type": "object", "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 64}},
                  "required": ["name"], "additionalProperties": False}, "read", _skill)]
