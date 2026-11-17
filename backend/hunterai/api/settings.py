from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Response
from typing import Literal

from pydantic import BaseModel, ValidationError

from .. import answer_check, caido, config, engine, envfile, notify, permissions, subagents
from ..tools import on_demand
from ..providers import registry
from . import providers as providers_api
from ..repo import settings
from ..tools import runner
from .deps import default_provider_model, get_conn
from .. import upstream

router = APIRouter(prefix="/api/settings")

KEY_NAMES = ["ABLIT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
             "GEMINI_API_KEY"]
SECRET_NAMES = [*KEY_NAMES, notify.KEY]      # what Settings may write to the .env file


class KeyIn(BaseModel):
    value: str


class SettingsIn(BaseModel):
    default_provider: str | None = None
    default_model: str | None = None
    fallback_model: str | None = None
    permissions: dict | None = None
    max_turns: int | None = None
    swarm_width: int | None = None          # subagents of one swarm that work at the same time
    decision_provider: str | None = None    # "" or null: use the model new chats use
    decision_model: str | None = None
    decision_gate: bool | None = None
    answer_check: bool | None = None        # hold each final answer against the run's tool results
    mcp_on_demand: bool | None = None       # keep the tools of large MCP servers out of the tool list (mcp_find, mcp_call)
    notify_findings: bool | None = None     # announce positive findings to the Discord webhook
    notify_min_severity: Literal["info", "low", "medium", "high", "critical"] | None = None
    kb_redact: bool | None = None
    vision_model: str | None = None         # "" or null: the first local model that reads images
    browser_via_caido: bool | None = None   # the browser tools' traffic goes through Caido and is recorded there
    upstream_proxy: str | None = None       # the owner's own HTTP proxy for the agent's web traffic; "" for none
    upstream_proxy_intercepts: bool | None = None   # it reads https: accept the certificates it presents
    decision_engine: bool | None = None     # the built-in decide_* tools and the risk gate (off: another engine is used)


def _machine() -> dict:
    """What this machine has, next to the settings that depend on it: processor cores and
    memory in GB. None for what cannot be read here."""
    try:
        memory = round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 2**30, 1)
    except (AttributeError, OSError, ValueError):
        memory = None
    return {"cores": os.cpu_count(), "memory_gb": memory}


async def _view(conn) -> dict:
    provider, model = await default_provider_model(conn)
    up = await upstream.get(conn)
    return {
        "default_provider": provider,
        "default_model": model,
        "fallback_model": await settings.get(conn, "fallback_model",
                                             engine.DEFAULT_FALLBACK_MODEL),
        "keys": {name: bool(config.env(name)) for name in KEY_NAMES},
        "permissions": await settings.get(conn, "permissions", {}) or {},
        "max_turns": await settings.get(conn, "max_turns", engine.DEFAULT_MAX_TURNS),
        "swarm_width": await subagents.swarm_width(conn),
        "swarm_width_max": subagents.SWARM_MAX_TASKS,
        "machine": _machine(),
        "presets": permissions.PRESETS,
        "decision_provider": await settings.get(conn, "decision_provider") or None,
        "decision_model": await settings.get(conn, "decision_model") or None,
        "decision_gate": bool(await settings.get(conn, "decision_gate", runner.GATE_DEFAULT)),
        "answer_check": bool(await settings.get(conn, "answer_check", answer_check.DEFAULT)),
        "mcp_on_demand": bool(await settings.get(conn, "mcp_on_demand", on_demand.DEFAULT)),
        "notify_findings": bool(await settings.get(conn, "notify_findings", notify.DEFAULT_ON)),
        "notify_min_severity": await settings.get(conn, "notify_min_severity", notify.DEFAULT_MIN),
        "notify_webhook_set": notify.webhook() is not None,
        "decision_engine": bool(await settings.get(conn, "decision_engine", runner.ENGINE_DEFAULT)),
        "kb_redact": bool(await settings.get(conn, "kb_redact", True)),
        "vision_model": await settings.get(conn, "vision_model") or None,
        "browser_via_caido": bool(await settings.get(conn, "browser_via_caido", False)),
        "caido_available": caido.available(),
        "upstream_proxy": up.url if up else "",
        "upstream_proxy_intercepts": bool(up and up.intercepts),
    }


@router.get("")
async def get_settings(conn=Depends(get_conn)):
    return await _view(conn)


async def _put_proxy(conn, fields: dict) -> None:
    """The owner's proxy and its switch, checked as a whole before anything is written. Caido's switch is
    read with them because the two ways out are not combined."""
    typed = fields.pop("upstream_proxy", None) if "upstream_proxy" in fields else None
    given = "upstream_proxy_intercepts" in fields and fields["upstream_proxy_intercepts"] is not None
    reads = fields.pop("upstream_proxy_intercepts", None)
    was = await upstream.get(conn)
    try:
        address = upstream.parse(typed) if typed is not None else (was and (was.host, was.port))
    except upstream.ProxyError as e:
        raise HTTPException(400, f"proxy: {e}") from None
    caido_on = fields["browser_via_caido"] if fields.get("browser_via_caido") is not None \
        else bool(await settings.get(conn, "browser_via_caido", False))
    if address and caido_on:
        raise HTTPException(400, "The browser and web_fetch already go through Caido. Switch that off to use another proxy."
                            if typed is not None else
                            "A proxy is set for the agent's web traffic. Clear it to send the traffic through Caido instead.")
    intercepts = bool(reads) if given else bool(was and was.intercepts)
    if address and intercepts and not upstream.on_this_machine(address[0]):
        if given and reads:
            raise HTTPException(400, "Only a proxy on this machine may read https: one elsewhere has to pass the site's "
                                "certificate on, and it is checked.")
        intercepts = False               # the proxy moved to another machine: its switch does not move with it
    await settings.put(conn, "upstream_proxy", upstream.Upstream(*address).url if address else "")
    await settings.put(conn, "upstream_proxy_intercepts", bool(address) and intercepts)


@router.put("")
async def put_settings(body: SettingsIn, conn=Depends(get_conn)):
    fields = body.model_dump(exclude_unset=True)
    if {"upstream_proxy", "upstream_proxy_intercepts"} & fields.keys() or fields.get("browser_via_caido"):
        await _put_proxy(conn, fields)
    if "permissions" in fields:
        try:
            policy = permissions.Policy(**(fields.pop("permissions") or {}))
        except (ValidationError, TypeError) as e:
            raise HTTPException(400, f"invalid permissions: {str(e).splitlines()[0]}")
        await settings.put(conn, "permissions", policy.model_dump(exclude_defaults=True))
    if "max_turns" in fields:
        turns = fields.pop("max_turns")
        if not isinstance(turns, int) or not 1 <= turns <= engine.MAX_TURNS_LIMIT:
            raise HTTPException(400, f"max_turns must be 1 to {engine.MAX_TURNS_LIMIT}")
        await settings.put(conn, "max_turns", turns)
    if "swarm_width" in fields:
        width = fields.pop("swarm_width")
        if not isinstance(width, int) or not 1 <= width <= subagents.SWARM_MAX_TASKS:
            raise HTTPException(400, f"swarm_width must be 1 to {subagents.SWARM_MAX_TASKS}")
        await settings.put(conn, "swarm_width", width)
    for key in ("notify_findings", "notify_min_severity"):
        if fields.get(key) is not None:
            await settings.put(conn, key, fields[key])
        fields.pop(key, None)
    if fields.get("mcp_on_demand") is not None:
        await settings.put(conn, "mcp_on_demand", bool(fields["mcp_on_demand"]))
    fields.pop("mcp_on_demand", None)
    if fields.get("answer_check") is not None:
        await settings.put(conn, "answer_check", bool(fields["answer_check"]))
    fields.pop("answer_check", None)
    if fields.get("decision_gate") is not None:
        await settings.put(conn, "decision_gate", bool(fields["decision_gate"]))
    fields.pop("decision_gate", None)
    if fields.get("kb_redact") is not None:
        await settings.put(conn, "kb_redact", bool(fields["kb_redact"]))
    fields.pop("kb_redact", None)
    if fields.get("decision_engine") is not None:
        await settings.put(conn, "decision_engine", bool(fields["decision_engine"]))
    fields.pop("decision_engine", None)
    if fields.get("browser_via_caido") is not None:
        await settings.put(conn, "browser_via_caido", bool(fields["browser_via_caido"]))
    fields.pop("browser_via_caido", None)
    for key in ("decision_provider", "decision_model", "vision_model"):
        if key in fields:
            await settings.put(conn, key, (fields.pop(key) or "").strip())
    for key, value in fields.items():
        if value:
            await settings.put(conn, key, value)
    return await _view(conn)


@router.put("/keys/{name}", status_code=204)
async def put_key(name: str, body: KeyIn):
    if name not in SECRET_NAMES:
        raise HTTPException(400, "unknown key name")
    if not body.value.strip():
        raise HTTPException(400, "value is empty")
    if name == notify.KEY and not notify.valid(body.value.strip()):
        raise HTTPException(400, "that is not a Discord webhook address (https://discord.com/api/webhooks/...)")
    try:
        envfile.set_var(config.env_file(), name, body.value)
    except ValueError as e:
        raise HTTPException(400, str(e))
    registry._instances.clear()  # adapters pick the new key up on next use
    providers_api.clear_cache()
    return Response(status_code=204)
