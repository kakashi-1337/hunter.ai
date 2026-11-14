"""A stdio MCP server whose tools come from a JSON file, for scanner tests.

    python fake_mcp_defs_server.py defs.json      # [{"name": ..., "description": ...}, ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("defs")


def _make(name: str):
    def tool(text: str = "") -> str:
        return f"{name}:{text}"
    tool.__name__ = name
    return tool


for d in json.loads(Path(sys.argv[1]).read_text()):
    mcp.tool(name=d["name"], description=d["description"])(_make(d["name"]))

if __name__ == "__main__":
    mcp.run("stdio")
