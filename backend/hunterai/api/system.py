"""What the owner sees about the machine and the services around HunterAI: the processor
temperature and the loaded local models, a checkup of what is installed, and what Caido
has recorded. Everything here reads; only `POST /api/caido/start` starts something."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import activity, caido, config, diagnostics, engine, mcp_manager, rea_tools, stale, thermal, toolbox
from . import providers as providers_api
from . import settings as settings_api
from .deps import get_conn
from ..kb import embed
from ..providers import colibri, ollama, registry
from ..repo import settings as store
from ..tools import caido_tools
from ..tools import registry as tools

router = APIRouter(prefix="/api")

PROBE = httpx.Timeout(2.0, connect=1.0)
LOCAL = "ollama"
# The services that run beside HunterAI. One that answered before and does not answer now has stopped, and
# the page says so; the start script only says it in the terminal it ran in. Programs and folders are not
# watched: what is missing there is the owner's doing, and the setup check lists it.
WATCHED = ("ollama", "qdrant", "searxng")
ANSWERED = "answered:"      # settings key: when the service last answered
IN_USE = 7 * 86400          # a service that has not answered for this long is no longer taken to be in use
LOOK_EVERY = 60.0           # seconds between two looks at the services on behalf of the status
REASON = 160                # characters of an MCP server's error that the setup check shows
HIDDEN = "stopped:hidden"   # settings key: the stopped parts the owner asked not to be told about again
ROOM = 8192                 # tokens a local window should have left for the chat beside the rules and the tools


_ENTRY = re.compile(r"""<script\b[^>]*\bsrc=["'](/assets/[^"']+)["']""")
_page: tuple[tuple | None, str | None] = (None, None)       # (what the file was when it was read, the script it names)


def page_script() -> str | None:
    """The script the built interface starts with, as its index.html names it; None while there is no build. A page
    that runs another one was opened before the interface was built again, and says to reload itself."""
    global _page
    try:
        index = config.dist_dir() / "index.html"
        seen = index.stat()
        mark = (str(index), seen.st_mtime_ns, seen.st_size)
        if _page[0] != mark:
            found = _ENTRY.search(index.read_text(encoding="utf-8", errors="replace"))
            _page = (mark, found.group(1) if found else None)
        return _page[1]
    except OSError:
        return None


def forget(app) -> None:
    """Nothing is held about the services: the next status looks again."""
    app.state.looked, app.state.stopped, app.state.stopped_now = None, [], []


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


def _loaded(m: dict) -> dict:
    """A model Ollama holds in memory: how much of it, how much of that on the GPU, where it runs in the words of
    `ollama ps`, and the window it was loaded with (None from an Ollama that does not say)."""
    window = m.get("context_length")
    return {"name": str(m.get("name") or m.get("model") or "?"), "vram_mb": round((m.get("size_vram") or 0) / 2**20),
            "size_mb": round((m.get("size") or 0) / 2**20), "processor": ollama.processor(m),
            "context": window if isinstance(window, int) and window > 0 else None}


def _same(a: str, b: str) -> bool:
    return a == b or a + ":latest" == b or a == b + ":latest"


@router.get("/system/local")
async def local_model(conn=Depends(get_conn)):
    """The local model in use (the fallback model) and its window: what the model takes at most, what a token
    of window costs in memory for it, how much of the window HunterAI's rules and tools take, the window that
    leaves room for a chat, and how Ollama runs the model now. For the hint beside the window setting."""
    model = await store.get(conn, "fallback_model", engine.DEFAULT_FALLBACK_MODEL)
    tags, ps = await asyncio.gather(_fetch(ollama.host() + "/api/tags"), _fetch(ollama.host() + "/api/ps"))
    names = [str(m.get("name")) for m in (tags or {}).get("models") or [] if isinstance(m, dict)]
    installed = any(_same(n, model) for n in names)
    said = await registry.get(LOCAL).described(model) if installed else None
    limit, cost = said or (None, None)
    setting = ollama.default_num_ctx()
    before = await overhead(conn)
    now = next((_loaded(m) for m in (ps or {}).get("models") or [] if isinstance(m, dict) and _same(str(m.get("name")), model)), None)
    return {"answers": tags is not None, "model": model, "installed": installed, "window": min(setting, limit) if limit else setting,
            "limit": limit, "bytes_per_token": cost,
            "overhead": {k: before[k] for k in ("tokens", "tools", "on_demand")},
            "enough": -(-(before["tokens"] + ROOM) // 4096) * 4096,
            "loaded": {k: now[k] for k in ("processor", "context", "size_mb")} if now else None}


@router.get("/system/colibri")
async def colibri_server(conn=Depends(get_conn)):
    """What the colibri server at the address in Settings says of itself, for the line under that address: whether it
    answers, the model it serves, its context, and whether that holds HunterAI's rules and tools with room for a chat
    (`enough`: the context to start it with when it does not). The server is asked anew, and so is the next list of
    models for the page: the owner looks here after he started or changed it."""
    seen = await registry.get(colibri.NAME).served(fresh=True)
    providers_api.forget(colibri.NAME)
    out = {"url": seen.at, "state": seen.state, "said": "" if seen.state in ("ok", "off") else colibri.why(seen),
           "model": None, "context": None, "images": False, "overhead": None, "enough": None}
    if seen.state == "ok":
        model, before = seen.models[0], await overhead(conn)
        out.update(model=model.id, context=model.context_window, images=model.supports_images, overhead=before["tokens"],
                   enough=colibri.context_to_start(before["tokens"] + ROOM, model.context_window))
    return out


async def _held_by_colibri() -> dict | None:
    """The model a colibri server holds now: it is loaded in a process of its own, which Ollama knows nothing of.
    None without an address, and when what is there serves no model to chat with. The provider keeps what the
    server said for half a minute, so the line that asks every few seconds asks colibri that often at most."""
    if not colibri.address():
        return None
    seen = await registry.get(colibri.NAME).served()
    if seen.state != "ok":
        return None
    return {"model": seen.models[0].id, "context": seen.models[0].context_window}


@router.get("/system/status")
async def status(request: Request, conn=Depends(get_conn)):
    """Cheap enough to ask every few seconds. `local_models` is null when Ollama does not answer; `colibri` is the
    model a colibri server holds, null when none does. `gpu_temp` is null on a machine whose card cannot be read,
    and while no local model is loaded. `stopped` names the services that answered before and do
    not now; they are looked at once a minute. `page` is the script the built interface starts with: a page that
    runs another one is older than this server."""
    ps = await _fetch(ollama.host() + "/api/ps")
    models = None if ps is None else [_loaded(m) for m in ps.get("models") or [] if isinstance(m, dict)]
    looked = getattr(request.app.state, "looked", None)
    if looked is None or time.time() - looked >= LOOK_EVERY:
        await checkup(request, conn)
    held = await _held_by_colibri()
    # Asking the card starts a program, and wakes a card that a laptop has put to sleep: only while a model is in memory.
    card = await thermal.gpu_temp() if models or held else None
    return {"cpu_temp": thermal.cpu_temp(), "cpu_limit": thermal.limit(), "gpu_temp": card, "gpu_limit": thermal.gpu_limit(),
            "local_models": models, "colibri": held,
            "restart_needed": stale.restart_needed(), "stopped": request.app.state.stopped, "page": page_script()}


def _browser_installed() -> bool:
    return importlib.util.find_spec("camoufox") is not None


def _item(id_: str, label: str, ok: bool, detail: str, fix: str, required: bool = False) -> dict:
    return {"id": id_, "label": label, "ok": ok, "required": required, "detail": detail, "fix": "" if ok else fix,
            "stopped": False}


def _reason(error: str | None) -> str:
    lines = (error or "").strip().splitlines()
    return lines[0].strip()[:REASON] if lines else ""


def _mcp_item() -> tuple[dict, list[str]]:
    """The line of the MCP servers, and the names of the ones that are switched on and gave up: the model then
    works with fewer tools than the owner thinks it has."""
    label, manager = "MCP servers (tools from other programs)", mcp_manager.get()
    if manager is None:
        return _item("mcp", label, True, "not started" if config.mcp_enabled() else "switched off with HUNTERAI_MCP=off", ""), []
    servers = manager.status()
    dead = [s for s in servers if s.state == "dead"]
    parts = []
    if dead:
        parts.append("not working: " + ", ".join(s.name + (f" ({_reason(s.error)})" if _reason(s.error) else "") for s in dead))
    for word, states in (("running", ("ready",)), ("starting", ("starting", "restarting"))):
        names = [s.name for s in servers if s.state in states]
        if names:
            parts.append(f"{word}: " + ", ".join(names))
    item = _item("mcp", label, not dead, "; ".join(parts) or "none switched on",
                 'Open Settings and press "Manage MCP servers": the reason stands beside the server. Switch it off and on '
                 "there to start it again, or leave it off if you do not use it.")
    item["stopped"] = bool(dead)
    return item, [s.name for s in dead]


async def _answered(conn, name: str) -> float | None:
    """When the service last answered, as far as the store knows."""
    last = await store.get(conn, ANSWERED + name)
    return float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) else None


async def _mark_stopped(conn, items: list[dict]) -> None:
    """Remember when each watched service last answered, and mark the ones that did within the last week and
    do not now."""
    now = time.time()
    for item in items:
        if item["id"] not in WATCHED:
            continue
        last = await _answered(conn, item["id"])
        if item["ok"]:
            if last is None or now - last >= 3600:                  # written once an hour, not at every look
                await store.put(conn, ANSWERED + item["id"], now)
        else:
            item["stopped"] = last is not None and now - last <= IN_USE


async def _announced(conn, items: list[dict], dead: list[str]) -> tuple[list[dict], list[str]]:
    """What the page says is not working, and every part that is stopped now (a service by its id, an MCP
    server as mcp:<name>). The parts the owner hid are left out for as long as they stay stopped: one that
    has run again is no longer hidden, so its next stop is said."""
    now = [i["id"] for i in items if i["stopped"] and i["id"] != "mcp"] + [f"mcp:{name}" for name in dead]
    stored = await store.get(conn, HIDDEN, [])
    hidden = [h for h in stored if h in now] if isinstance(stored, list) else []
    if hidden != stored:
        await store.put(conn, HIDDEN, hidden)
    said = [{"id": i["id"], "label": i["label"]} for i in items if i["stopped"] and i["id"] != "mcp" and i["id"] not in hidden]
    servers = [name for name in dead if f"mcp:{name}" not in hidden]
    if servers:
        said.append({"id": "mcp", "label": f"MCP server{'s' if len(servers) > 1 else ''} {', '.join(servers)}"})
    return said, now


@router.get("/system/checkup")
async def checkup(request: Request, conn=Depends(get_conn)):
    """What a new install has and lacks. Only a model to talk to is required; the rest switches features on."""
    tags, qdrant, searx = await asyncio.gather(
        _fetch(ollama.host() + "/api/tags"), _fetch(config.qdrant_url() + "/"), _fetch(config.searxng_url() + "/"))
    names = [str(m.get("name")) for m in (tags or {}).get("models") or [] if isinstance(m, dict) and m.get("name")]
    chat_models = [n for n in names if not ollama._is_embedder(n)]
    keyed = []
    for p in registry.all():
        try:
            if p.name not in (LOCAL, colibri.NAME) and p.configured():
                keyed.append(p.name)
        except Exception:  # noqa: BLE001 - one broken provider must not hide the others
            pass
    # colibri has an address or it is not used at all; with one, it is a model to talk to only while it serves one.
    served = await registry.get(colibri.NAME).served() if colibri.address() else None
    serves = served.models[0].id if served and served.state == "ok" else None
    embedder = embed.DEFAULT_EMBEDDER.partition(":")[2]
    have = []
    if keyed:
        have.append("ready: " + ", ".join(keyed))
    if chat_models:
        have.append("local: " + ", ".join(chat_models[:4]) + (f" and {len(chat_models) - 4} more" if len(chat_models) > 4 else ""))
    if serves:
        have.append(f"colibri: {serves}")
    binary = caido.binary() if caido.available() and caido.managed() else None
    programs = toolbox.installed()
    rea_found = rea_tools.found()
    caido_off = (os.environ.get("HUNTERAI_CAIDO") or "").lower() == "off"
    items = [
        _item("model", "A model to talk to", bool(keyed or chat_models or serves), "; ".join(have) or "no API key and no local model",
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
    if served is not None:
        items.insert(2, _item("colibri", "colibri (a local model from memory and storage)", bool(serves),
                              f"serves {serves} at {served.at}" if serves else colibri.why(served),
                              "Start it (./start-here.sh in the colibri folder), or see its address and its key under "
                              "Settings, colibri."))
    mcp, dead = _mcp_item()
    items.append(mcp)
    await _mark_stopped(conn, items)
    request.app.state.looked = time.time()
    request.app.state.stopped, request.app.state.stopped_now = await _announced(conn, items, dead)
    return {"ready": all(i["ok"] for i in items if i["required"]), "items": items}


async def overhead(conn) -> dict:
    """What a new chat sends to a local model before a word of the conversation: HunterAI's rules and every tool
    definition, in tokens as they are counted for a local window. With many MCP tools this alone can be more than
    the window. {"tokens", "tools": listed for the model, "on_demand": reached through mcp_find, "window"}"""
    from .. import context, skills
    from ..modes import build_system
    from ..providers.base import ToolSpec
    from ..tools import on_demand, runner
    from ..types import Chat
    model = await store.get(conn, "fallback_model", engine.DEFAULT_FALLBACK_MODEL)
    chat = Chat(id="0" * 32, target_id=None, title="", mode="pentesting", provider=LOCAL, model=model, options={},
                active_leaf_id=None, created_at="", updated_at="")
    shown, later = await runner.for_model(conn, await runner.offered_tools(conn, chat))
    specs = [ToolSpec(t.name, t.description, t.parameters) for t in shown]
    system = build_system("pentesting", None, [s.name for s in specs], config.home() / "work" / chat.id)
    if later:
        system += "\n\n" + on_demand.index(later)
    try:
        system += "\n\n" + (await skills.system_block(conn, [], [s.name for s in specs]) or "")
    except Exception:  # noqa: BLE001 - the list of skills is a small part: the count goes on without it
        pass
    return {"tokens": int(context.size(system, [], specs) / ollama.CHARS_PER_TOKEN), "tools": len(specs),
            "on_demand": len(later), "window": ollama.default_num_ctx()}


@router.get("/system/overhead")
async def overhead_view(conn=Depends(get_conn)):
    return await overhead(conn)


@router.post("/system/stopped/hide")
async def hide_stopped(request: Request, conn=Depends(get_conn)):
    """The owner knows that these parts do not run (Ollama closed to free memory, Docker not started today):
    the page stops saying it until a part has run again. The setup check keeps marking them."""
    await checkup(request, conn)
    await store.put(conn, HIDDEN, request.app.state.stopped_now)
    request.app.state.stopped = []
    return {"stopped": []}


@router.get("/system/diagnostics")
async def diagnostics_text(request: Request, conn=Depends(get_conn)):
    """The state of this install as text for a report. See diagnostics.py for what is left out."""
    check = await checkup(request, conn)
    manager = mcp_manager.get()
    return {"text": diagnostics.text(check, await status(request, conn), await settings_api._view(conn),
                                     manager.status() if manager else None, tools.all(), local=await local_model(conn))}


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
async def caido_requests(filter: str = "", limit: int = 50, offset: int = 0, media: bool = False):  # noqa: A002 - the query name the UI sends
    """The recorded requests, without the pictures, fonts, sound and film unless `media`: `left_out` says how many."""
    await _running()
    try:
        rows, total, left_out, _ = await caido.listing(filter.strip() or None, limit, offset, media)
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
    return {"total": total, "rows": out, "left_out": left_out}


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
