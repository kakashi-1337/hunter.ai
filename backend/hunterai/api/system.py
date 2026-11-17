"""What the owner sees about the machine and the services around HunterAI: the processor
temperature and the loaded local models, a checkup of what is installed, and what Caido
has recorded. Everything here reads; only `POST /api/caido/start` starts something."""
from __future__ import annotations

import asyncio
import importlib.util
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from .. import activity, caido, config, rea_tools, stale, thermal, toolbox
from .deps import get_conn
from ..kb import embed
from ..providers import ollama, registry
from ..tools import caido_tools

router = APIRouter(prefix="/api")

PROBE = httpx.Timeout(2.0, connect=1.0)
LOCAL = "ollama"


async def _fetch(url: str) -> dict | None:
    """The JSON a local service answers with ({} when it answers something else); None when it does not answer."""
    try:
        async with httpx.AsyncClient(timeout=PROBE, trust_env=False) as client:
            r = await client.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code >= 500:
        return None
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@router.get("/system/status")
async def status():
    """Cheap enough to ask every few seconds. `local_models` is null when Ollama does not answer."""
    ps = await _fetch(ollama.host() + "/api/ps")
    models = None if ps is None else [
        {"name": str(m.get("name") or m.get("model") or "?"), "vram_mb": round((m.get("size_vram") or 0) / 2**20)}
        for m in ps.get("models") or [] if isinstance(m, dict)]
    return {"cpu_temp": thermal.cpu_temp(), "cpu_limit": thermal.limit(), "local_models": models,
            "restart_needed": stale.restart_needed()}


def _browser_installed() -> bool:
    return importlib.util.find_spec("camoufox") is not None


def _item(id_: str, label: str, ok: bool, detail: str, fix: str, required: bool = False) -> dict:
    return {"id": id_, "label": label, "ok": ok, "required": required, "detail": detail, "fix": "" if ok else fix}


@router.get("/system/checkup")
async def checkup():
    """What a new install has and lacks. Only a model to talk to is required; the rest switches features on."""
    tags, qdrant, searx = await asyncio.gather(
        _fetch(ollama.host() + "/api/tags"), _fetch(config.qdrant_url() + "/"), _fetch(config.searxng_url() + "/"))
    names = [str(m.get("name")) for m in (tags or {}).get("models") or [] if isinstance(m, dict) and m.get("name")]
    chat_models = [n for n in names if not ollama._is_embedder(n)]
    keyed = []
    for p in registry.all():
        try:
            if p.name != LOCAL and p.configured():
                keyed.append(p.name)
        except Exception:  # noqa: BLE001 - one broken provider must not hide the others
            pass
    embedder = embed.DEFAULT_EMBEDDER.partition(":")[2]
    have = []
    if keyed:
        have.append("ready: " + ", ".join(keyed))
    if chat_models:
        have.append("local: " + ", ".join(chat_models[:4]) + (f" and {len(chat_models) - 4} more" if len(chat_models) > 4 else ""))
    binary = caido.binary() if caido.available() and caido.managed() else None
    programs = toolbox.installed()
    rea_found = rea_tools.found()
    caido_off = (os.environ.get("HUNTERAI_CAIDO") or "").lower() == "off"
    items = [
        _item("model", "A model to talk to", bool(keyed or chat_models), "; ".join(have) or "no API key and no local model",
              f"Open Settings and enter an API key (it is written to {config.env_file()}), or start Ollama and pull "
              "a chat model. Then press Check again.", required=True),
        _item("ollama", "Ollama (local models, the fallback, look_at_image)", tags is not None,
              f"answers at {ollama.host()}" if tags is not None else f"no answer at {ollama.host()}",
              "Install Ollama and run `ollama serve`."),
        _item("embedder", "Embedding model (the knowledge base)", any(n.split(":")[0] == embedder for n in names),
              f"{embedder} is installed" if any(n.split(":")[0] == embedder for n in names) else f"{embedder} is not installed",
              f"Run `ollama pull {embedder}`."),
        _item("qdrant", "Qdrant (the knowledge base)", qdrant is not None,
              f"answers at {config.qdrant_url()}" if qdrant is not None else f"no answer at {config.qdrant_url()}",
              "Run `scripts/qdrant.sh` in the HunterAI folder (it needs Docker)."),
        _item("searxng", "SearXNG (web_search)", searx is not None,
              f"answers at {config.searxng_url()}" if searx is not None else f"no answer at {config.searxng_url()}",
              "Run `scripts/searxng.sh start` in the HunterAI folder (it needs Docker)."),
        _item("caido", "Caido (recorded requests, replay)", caido.available(),
              (str(binary) if binary else caido.base_url()) if caido.available()
              else "switched off with HUNTERAI_CAIDO=off" if caido_off else "caido-cli was not found",
              "Remove HUNTERAI_CAIDO=off and restart HunterAI." if caido_off
              else "Put caido-cli on your PATH, or set HUNTERAI_CAIDO_BIN to it, and restart HunterAI."),
        _item("browser", "Camoufox (the browser tools)", _browser_installed(),
              "the camoufox package is installed" if _browser_installed() else "the camoufox package is missing",
              "Run `uv sync` in backend/, then `camoufox fetch` once."),
        _item("toolbox", "Your tool folder (programs for the shell tool)", bool(programs),
              ", ".join(t["name"] for t in programs[:20]) + (f" and {len(programs) - 20} more" if len(programs) > 20 else "")
              if programs else "empty",
              f"Put programs such as nuclei, httpx, subfinder, katana (or links to them) in {toolbox.primary()}: "
              "the shell tool then finds them by name and the model is told they are there."),
        _item("rea", "Programs for the rea server (Ghidra, Hopper, JADX and others)", bool(rea_found),
              ", ".join(rea_found) if rea_found else "none given",
              f"Put or link them in {rea_tools.folder()}: ghidra/ (a Ghidra installation), jdk/, hopper/, ilspycmd, adb, "
              "and bin/ for programs found by name (jadx, rizin, apktool). Switch the rea server off and on afterwards."),
    ]
    return {"ready": all(i["ok"] for i in items if i["required"]), "items": items}


@router.get("/system/toolbox")
async def toolbox_view():
    tools = toolbox.installed()
    have = {t["name"] for t in tools}
    return {"folder": str(toolbox.primary()), "folders": [str(p) for p in toolbox.folders()], "tools": tools, "missing": [n for n in toolbox.KNOWN if n not in have]}


async def _caido_up() -> bool:
    async with caido._client() as client:
        return await caido._up(client)


async def _running() -> None:
    if not caido.available() or not await _caido_up():
        raise HTTPException(409, "Caido is not running")


@router.get("/caido/status")
async def caido_status():
    """Never starts Caido: the panel asks this every time it opens."""
    if not caido.available():
        return {"available": False, "running": False}
    if not await _caido_up():
        return {"available": True, "running": False, "url": caido.base_url()}
    try:
        return {"available": True, "running": True, **await caido.status()}
    except caido.CaidoError as e:
        return {"available": True, "running": True, "url": caido.base_url(), "error": str(e)}


@router.post("/caido/start")
async def caido_start():
    if not caido.available():
        raise HTTPException(409, "there is no Caido to start: caido-cli was not found")
    try:
        return {"available": True, "running": True, **await caido.status()}     # signs in, which starts it
    except caido.CaidoError as e:
        raise HTTPException(502, str(e))


@router.get("/caido/requests")
async def caido_requests(filter: str = "", limit: int = 50, offset: int = 0):  # noqa: A002 - the query name the UI sends
    await _running()
    try:
        rows, total = await caido.history(filter.strip() or None, limit, offset)
    except caido.CaidoError as e:
        raise HTTPException(400, str(e))
    out = []
    for r in rows:
        response = r.get("response") or {}
        port, tls = r.get("port"), bool(r.get("isTls"))
        at = "" if port in (443 if tls else 80, None) else f":{port}"
        query = f"?{r['query']}" if r.get("query") else ""
        out.append({"id": str(r.get("id")), "method": r.get("method"),
                    "url": f"{'https' if tls else 'http'}://{r.get('host')}{at}{r.get('path') or '/'}{query}",
                    "status": response.get("statusCode"), "length": response.get("length"),
                    "ms": response.get("roundtripTime"), "source": r.get("source"), "at": r.get("createdAt")})
    return {"total": total, "rows": out}


@router.get("/caido/requests/{rid}")
async def caido_request(rid: str):
    await _running()
    try:
        r = await caido.exchange(rid)
    except caido.CaidoError as e:
        raise HTTPException(400, str(e))
    if r is None:
        raise HTTPException(404, "no such request")
    response = (r.get("response") or {}).get("raw")
    return {"id": str(r.get("id")), "request": caido_tools.render(r.get("raw") or b""),
            "response": caido_tools.render(response) if response else None}


@router.get("/activity")
async def activity_summary(days: int = Query(7, ge=1, le=365), conn=Depends(get_conn)):
    """Calls, tokens, durations and failures of the last days. Counted on this machine; sent nowhere."""
    return await activity.summary(conn, days)
