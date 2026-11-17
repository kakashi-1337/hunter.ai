from __future__ import annotations

import asyncio

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .. import config, db, engine, mcp_manager, procs
from ..tools import native
from . import chats, findings, processes, providers, runs, search, settings, system, targets, tools, uploads
from . import kb as kb_api
from . import mcp_admin
from . import personas as personas_api
from . import skills as skills_api
from . import memory as memory_api

LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def _hostname(value: str) -> str:
    """Hostname of a Host header or an Origin URL, lowercased, without the port."""
    if "://" in value:
        return (urlsplit(value).hostname or "").lower()
    return value.rsplit(":", 1)[0].lower() if ":" in value else value.lower()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = await db.connect()
    try:
        await db.migrate(conn)
        from .. import upstream
        await upstream.remember(conn)       # proxy passwords are scrubbed from tool output from the first call on
        await engine.recover(conn)
        await procs.recover(conn)
    finally:
        await conn.close()
    providers.clear_cache()
    native.register_all()
    if config.mcp_enabled():
        # A server that cannot start does not stop the app; `read` then uses its fallback.
        await mcp_manager.start()
    from .. import caido
    rerouting = asyncio.create_task(caido.refresh())     # a Caido left running gets this run's filter as its way out
    try:
        yield
    finally:
        rerouting.cancel()
        # Each step runs even when the one before it raised or was cancelled, so process
        # groups and MCP children are always ended.
        try:
            await engine.shutdown()
        finally:
            try:
                await procs.shutdown()
            finally:
                try:
                    from .. import browser, hop
                    try:
                        await browser.shutdown()
                    finally:
                        await hop.shutdown()      # after the commands and the browser that used a hop are gone
                finally:
                    try:
                        from .. import caido
                        await caido.shutdown()
                    finally:
                        await mcp_manager.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="HunterAI", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def local_only(request, call_next):
        # The server listens on loopback only, but a web page elsewhere could still aim the
        # browser at it (DNS rebinding, cross-site POST). Refuse anything not addressed to,
        # and not coming from, localhost.
        if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        # Tools can now run commands, so only the app's own pages may call the API: any
        # other local web app (or a page on another port) is refused.
        origin = request.headers.get("origin")
        if origin is not None and origin not in config.allowed_origins():
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        path = request.url.path
        if (path == "/api" or path.startswith("/api/")) and \
                request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        return await call_next(request)

    for module in (targets, chats, runs, uploads, search, settings, providers, tools,
                   processes, findings, kb_api, mcp_admin, personas_api, skills_api, memory_api, system):
        app.include_router(module.router)

    @app.get("/{path:path}", include_in_schema=False)
    async def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        dist = config.dist_dir().resolve()
        index = dist / "index.html"
        if not index.is_file():
            return PlainTextResponse(
                "The HunterAI interface has not been built yet. Run scripts/start.sh "
                "(or scripts/dev.sh for development).", status_code=404)
        if path:
            try:
                candidate = (dist / path).resolve()
            except ValueError:  # a NUL byte in the path: no such file
                candidate = None
            # Only files that really live inside the build folder are ever served.
            if candidate and candidate.is_relative_to(dist) and candidate.is_file():
                return FileResponse(candidate)
            if path.startswith("assets/"):
                return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
