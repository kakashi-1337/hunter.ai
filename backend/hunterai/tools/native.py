"""Registers HunterAI's own tools. MCP tools register themselves through the manager."""
from __future__ import annotations

from . import browser_tools, caido_tools, cve, decide_tools, files, kb_tools, mind, on_demand, profundis, registry, search, shell, vision, web


def register_all() -> None:
    """Safe to call again: the native tools are replaced, other sources are left alone."""
    registry.unregister_source("native")
    for module in (files, shell, web, search, decide_tools, mind, kb_tools, browser_tools, profundis, vision, caido_tools, cve, on_demand):
        for tool in module.tools():
            registry.register(tool)
    from .. import subagents
    registry.register(subagents.tool())
    registry.register(subagents.swarm_tool())
    from . import skill_tool
    for tool in skill_tool.tools():
        registry.register(tool)
