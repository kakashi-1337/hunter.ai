from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import mcp_manager, permissions
from ..permissions import Policy
from ..repo import chats, settings
from ..tools import registry
from .deps import get_conn, not_found

router = APIRouter(prefix="/api")


@router.get("/tools")
async def list_tools(chat_id: str | None = None, conn=Depends(get_conn)):
    """Every tool the model could be offered, with what this chat's settings decide for it."""
    mode, chat_policy = "pentesting", Policy()
    if chat_id:
        chat = await chats.get(conn, chat_id)
        if not chat:
            raise not_found("chat")
        mode, chat_policy = chat.mode, Policy(**(chat.permissions or {}))
    global_policy = Policy(**(await settings.get(conn, "permissions", {}) or {}))
    out = []
    for t in registry.all():
        if not t.expose:
            continue
        decision, rule = permissions.base_decision(mode, t, global_policy, chat_policy)
        if decision == "ask" and permissions.auto_applies(rule, chat_policy):
            decision, rule = "allow", "auto"        # still asked where a call touches what always asks
        out.append({"name": t.name, "description": t.description, "risk": t.risk,
                    "source": t.source, "decision": decision, "rule": rule,
                    "offered": permissions.offered(mode, t, global_policy, chat_policy)
                    and (t.available is None or t.available())})
    return out


@router.get("/mcp")
async def mcp_status():
    manager = mcp_manager.get()
    _, error = mcp_manager.load_config()
    return await _mcp_view()


async def _mcp_view() -> dict:
    """`servers`: what is running. `configured`: every server that can be switched, with no
    environment values (they may hold credentials)."""
    manager = mcp_manager.get()
    servers, error = mcp_manager.load_config()
    return {"servers": manager.status() if manager else [], "error": error,
            "configured": [{"name": c.name, "enabled": c.enabled, "risk": c.risk,
                            "transport": c.transport, "expose": c.expose,
                            "bundled": c.name in mcp_manager.BUNDLED}
                           for c in servers.values()]}


class McpSwitch(BaseModel):
    enabled: bool


@router.put("/mcp/{name}")
async def mcp_switch(name: str, body: McpSwitch):
    """Turn one server on or off: written to mcp.json, and only that server restarts."""
    try:
        cfg = mcp_manager.set_enabled(name, body.enabled)
    except KeyError:
        raise not_found("MCP server")
    except ValueError as e:
        raise HTTPException(400, str(e))
    manager = mcp_manager.get()
    if manager is not None:
        await manager.replace(cfg)
    return await _mcp_view()
