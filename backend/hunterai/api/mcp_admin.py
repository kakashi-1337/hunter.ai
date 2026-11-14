"""MCP manager: inspect a server before adding it, install what was approved, add, change,
or remove servers by hand, and check that one starts. Environment values go in and are
never sent back."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .. import mcp_adder, mcp_manager, mcp_scan
from ..mcp_adder import AdderError
from .deps import get_conn, not_found

router = APIRouter(prefix="/api/mcp")
Risk = Literal["read", "write", "execute", "network"]


class InspectIn(BaseModel):
    source: str = Field(max_length=20_000)


class InstallIn(BaseModel):
    name: str
    risk: Risk = "execute"
    env: dict[str, str] = {}
    approved_install: list[list[str]]
    command: str | None = None
    args: list[str] | None = None


class ServerIn(BaseModel):
    name: str | None = None
    transport: Literal["stdio", "http"] | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str | None] | None = None      # null keeps the stored value of that name
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str | None] | None = None  # http only; null keeps the stored value of that name
    risk: Risk | None = None


def _public(record: dict) -> dict:
    return record


def _server_view(cfg: mcp_manager.ServerConfig) -> dict:
    return {"name": cfg.name, "transport": cfg.transport, "command": cfg.command, "args": cfg.args, "cwd": cfg.cwd,
            "url": cfg.url, "risk": cfg.risk, "enabled": cfg.enabled, "expose": cfg.expose,
            "bundled": cfg.name in mcp_manager.BUNDLED, "env_names": sorted(cfg.env), "header_names": sorted(cfg.headers)}


@router.post("/inspect")
async def inspect(body: InspectIn):
    """Fetch the code (it is not run), work out how it would start, and check it."""
    try:
        return _public(await mcp_adder.inspect(body.source))
    except AdderError as e:
        raise HTTPException(400, str(e))


@router.get("/inspections/{iid}")
async def get_inspection(iid: str):
    try:
        return _public(mcp_adder.load(iid))
    except KeyError:
        raise not_found("inspection")


REVIEW_QUESTIONS = {
    "malicious": {"type": "noul", "instructions": "Judging only by these findings, is this MCP server likely to be "
                  "malicious or to leak the user's data or credentials?"},
    "advice": {"type": "choice", "instructions": "What should the owner do?",
               "criteria": {"install": "Nothing here is a reason not to install it",
                            "read_first": "Read the flagged code before installing",
                            "do_not_install": "The findings show behaviour that should not be installed"}},
}


@router.post("/inspections/{iid}/review")
async def review(iid: str, conn=Depends(get_conn)):
    """A model's reading of the checker's findings. Advice from a model, stored with the
    inspection and labelled as such; the findings themselves are the evidence."""
    from ..decision import engine as de
    try:
        record = mcp_adder.load(iid)
    except KeyError:
        raise not_found("inspection")
    report = record.get("report")
    if not report:
        raise HTTPException(400, "there is no code check to review for this inspection")
    lines = [f"MCP server: {record.get('label') or record['source']}",
             f"Proposed start: {record['proposal'].get('command')} {' '.join(record['proposal'].get('args') or [])}"[:400],
             f"Files: {report['files']}; findings by severity: {report['counts']}",
             f"Dependencies with known vulnerabilities: {len((record.get('audit') or {}).get('vulnerable') or [])}", "FINDINGS:"]
    lines += [f"- [{f['severity']}] {f['title']} ({f['file']}:{f['line']}): {f['excerpt']}" for f in report["findings"][:60]]
    res = await de.decide(conn, "\n".join(lines)[:de.MAX_STATE_CHARS], REVIEW_QUESTIONS)
    a = res["answers"]
    out = {"invalid": bool(res.get("invalid")), "model": f"{res.get('provider')}/{res.get('model')}",
           "malicious_probability": a["malicious"]["probability"],
           "advice": "read_first" if res.get("invalid") else a["advice"]["choice"],
           "advice_confidence": a["advice"]["confidence"], "error": res.get("error")}
    record["review"] = out
    mcp_adder._save(record)
    return out


@router.post("/inspections/{iid}/install")
async def install(iid: str, body: InstallIn):
    """The owner's approval: run the shown install steps and add the server, switched off."""
    try:
        cfg = await mcp_adder.install(iid, name=body.name, risk=body.risk, env=body.env,
                                      approved_install=body.approved_install, command=body.command, args=body.args)
    except KeyError:
        raise not_found("inspection")
    except AdderError as e:
        raise HTTPException(400, str(e))
    manager = mcp_manager.get()
    if manager is not None:
        await manager.replace(cfg)       # known to the manager, still off
    return _server_view(cfg)


@router.get("/servers")
async def list_servers():
    servers, error = mcp_manager.load_config()
    return {"servers": [_server_view(c) for c in servers.values()], "error": error}


@router.post("/servers")
async def add_server(body: ServerIn):
    if not body.name:
        raise HTTPException(400, "a server needs a name")
    fields = body.model_dump(exclude_unset=True, exclude={"name"})
    try:
        cfg = mcp_manager.put_server(body.name, {**fields, "enabled": False}, create=True)
    except FileExistsError:
        raise HTTPException(409, f'a server named "{body.name}" already exists')
    except ValueError as e:
        raise HTTPException(400, str(e))
    manager = mcp_manager.get()
    if manager is not None:
        await manager.replace(cfg)
    return _server_view(cfg)


@router.patch("/servers/{name}")
async def change_server(name: str, body: ServerIn):
    fields = body.model_dump(exclude_unset=True, exclude={"name"})
    try:
        cfg = mcp_manager.put_server(name, fields, create=False)
    except KeyError:
        raise not_found("MCP server")
    except ValueError as e:
        raise HTTPException(400, str(e))
    manager = mcp_manager.get()
    if manager is not None:
        await manager.replace(cfg)       # restarts it with the new settings when it is on
    return _server_view(cfg)


@router.delete("/servers/{name}", status_code=204)
async def delete_server(name: str):
    try:
        mcp_manager.remove_server(name)
    except KeyError:
        raise not_found("MCP server")
    except ValueError as e:
        raise HTTPException(400, str(e))
    manager = mcp_manager.get()
    if manager is not None:
        await manager.remove(name)
    mcp_scan.forget(name)
    return Response(status_code=204)


class ApproveIn(BaseModel):
    tools: dict[str, str] = Field(min_length=1, max_length=500)     # tool name -> the digest that was shown


def _scan_view(name: str) -> dict:
    servers, _ = mcp_manager.load_config()
    if name not in servers:
        raise not_found("MCP server")
    manager = mcp_manager.get()
    status = next((s for s in manager.status() if s.name == name), None) if manager is not None else None
    return {"name": name, "state": status.state if status else "disabled",
            "report": manager.scan(name) if manager is not None else None}


@router.get("/servers/{name}/scan")
async def scan_server(name: str):
    """What the scanner made of the tool definitions the server sent when it last started."""
    return _scan_view(name)


@router.post("/servers/{name}/approve")
async def approve_tools(name: str, body: ApproveIn):
    """The owner accepts held tool definitions as shown; the server starts again with them."""
    manager = mcp_manager.get()
    try:
        if manager is None:
            raise KeyError(name)
        await manager.approve(name, body.tools)
    except KeyError:
        raise not_found("MCP server")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return _scan_view(name)


@router.post("/servers/{name}/check")
async def check_server(name: str):
    """Health check: (re)start it if it is on, time a ping, and read what it printed."""
    servers, _ = mcp_manager.load_config()
    cfg = servers.get(name)
    if cfg is None:
        raise not_found("MCP server")
    manager = mcp_manager.get()
    status, ping = None, None
    if manager is not None and cfg.enabled:
        ping = await manager.ping(name)
        if ping is None:                 # only a server that does not answer is started again
            await manager.replace(cfg)
            ping = await manager.ping(name)
        status = next((s.model_dump() for s in manager.status() if s.name == name), None)
    tail = mcp_adder.log_tail(name)
    state = status["state"] if status else ("disabled" if not cfg.enabled else "not started")
    return {"name": name, "state": state, "tools": (status or {}).get("tools", []), "error": (status or {}).get("error"),
            "ping_ms": ping, "log_tail": tail,
            "hints": mcp_adder.hints(f"{(status or {}).get('error') or ''}\n{tail}") if state != "ready" else []}
