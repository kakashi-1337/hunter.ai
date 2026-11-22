"""MCP client: starts the configured servers and registers their tools in the one registry,
so an MCP tool passes the same permission engine and trajectory logging as a native tool.

Each server is owned by one asyncio task that opens the transport and the session and
leaves them again; nothing else enters or exits them.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import signal
import shutil
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CONNECTION_CLOSED
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from . import config, mcp_scan, rea_tools
from .tools import registry, resolve
from .tools.base import HostUse, PathUse, Prepared, Tool, ToolContext, ToolError, ToolResult
from .types import Risk

INIT_TIMEOUT = 20.0
CALL_TIMEOUT = 120.0
CANCEL_GRACE = 5.0
MAX_RESTARTS = 5            # after an unexpected end of a server that had come up, within RESTART_WINDOW
RESTART_WINDOW = 600.0      # seconds; a server that stops more often than that is left alone
MAX_FAILED_STARTS = 1       # in a row: a server that does not come up is tried once more
MAX_FORCED_RESTARTS = 5     # after a call that timed out or stayed busy
ALIVE_EVERY = 5.0           # seconds between two looks at whether the process of a stdio server is still there
ENDED_IDLE = "its process ended while nobody called it"
WALK_DEPTH = 5
MAX_TOOL_PAGES = 50
MAX_TOOLS = 2000
PATH_KEYS = {"path", "file", "file_path", "filepath", "filename", "dir", "directory", "cwd",
             "out_dir", "root"}
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}   # http only; sent with every request (an API key, usually), never shown again
    risk: Risk = "execute"      # an unknown server asks before every call
    enabled: bool = True
    expose: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME.match(v):
            raise ValueError("server names are lowercase letters, digits, and _ (max 24)")
        return v

    @model_validator(mode="after")
    def _transport(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError("a stdio server needs a command")
        if self.transport == "http" and not self.url:
            raise ValueError("an http server needs a url")
        if self.transport != "http":
            if self.headers:
                raise ValueError("headers belong to an http server")
            return self
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the url of an http server starts with http:// or https://")
        if parts.username or parts.password:
            raise ValueError("put the key in a header, not as a user name or password in the url")
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if self.headers and parts.scheme != "https" and not local:
            raise ValueError("headers are only sent over https (or to this machine)")
        for k, v in self.headers.items():
            if not _HEADER_NAME.match(k) or not v or any(c in v for c in "\r\n\x00") or len(v) > 8192:
                raise ValueError(f"header {k[:40]!r} has a name or value that cannot be sent")
        return self


def default_servers() -> dict[str, ServerConfig]:
    """The bundled servers. The owner's file chunker is the default file reader: its own
    tools are never offered to the model, the native `read` tool is the only way to them.
    `rea` and `context7` are off until the owner turns them on (each adds many tools to
    every request)."""
    npx = shutil.which("npx") or "npx"
    return {
        "chunker": ServerConfig(
            name="chunker", command=sys.executable, args=[str(config.chunker_path())],
            env={"CHUNKER_MAX_MB": "200"}, risk="read", expose=False),
        "rea": ServerConfig(name="rea", command=npx, args=["-y", "rea-agents@6.7.0", "mcp"],
                            env=rea_tools.env(), risk="execute", enabled=False),   # Ghidra, Hopper and the rest from ~/.hunterai/rea
        "context7": ServerConfig(name="context7", command=npx,
                                 args=["-y", "@upstash/context7-mcp"], risk="network",
                                 enabled=False),
    }


BUNDLED = ("chunker", "rea", "context7")


def _read_file() -> dict:
    path = config.mcp_file()
    data: dict = {"servers": {}}
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("servers"), dict):
                raise ValueError("'servers' must be an object")
        except (OSError, ValueError) as e:
            raise ValueError(f"{path.name} is not valid: {e}") from None
    return data


def _write_file(data: dict) -> None:
    path = config.mcp_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # env may hold credentials
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def set_enabled(name: str, enabled: bool) -> ServerConfig:
    """Write the switch into mcp.json and return the server's new configuration.
    KeyError for a server that is not configured; ValueError when mcp.json cannot be read."""
    data = _read_file()
    if name not in load_config()[0]:
        raise KeyError(name)
    entry = data["servers"].get(name)
    data["servers"][name] = {**(entry if isinstance(entry, dict) else {}), "enabled": enabled}
    _write_file(data)
    return load_config()[0][name]


def problem(e: Exception) -> str:
    """The reason inside a validation error, without the input value."""
    if isinstance(e, ValidationError):
        first = e.errors(include_input=False, include_url=False)[0]
        where = ".".join(str(x) for x in first.get("loc", ()))
        return f"{where}: {first['msg']}" if where else str(first["msg"])
    return str(e)[:300]


def _origin(url) -> tuple:
    parts = urlsplit(str(url or ""))
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def put_server(name: str, fields: dict, *, create: bool) -> ServerConfig:
    """Add a server, or change one that is not bundled. `fields` are ServerConfig fields; an
    `env` value of None keeps the stored value of that name (values are never read back).
    KeyError: unknown server on change; FileExistsError: name taken on add; ValueError: invalid."""
    if name in BUNDLED:
        raise ValueError("a bundled server can only be switched on or off")
    data = _read_file()
    old = data["servers"].get(name)
    if create and (old is not None or name in load_config()[0]):
        raise FileExistsError(name)
    if not create and not isinstance(old, dict):
        raise KeyError(name)
    merged = {**(old or {}), **{k: v for k, v in fields.items() if k not in ("env", "headers")}}
    moved = bool(old and old.get("headers")) and _origin(merged.get("url")) != _origin(old.get("url"))
    for secret in ("env", "headers"):
        if secret in fields:
            kept = (old or {}).get(secret) or {}
            if secret == "headers" and moved and any(v is None for v in (fields[secret] or {}).values()):
                raise ValueError("the url now points at another host: enter the header values again")
            merged[secret] = {k: (kept.get(k, "") if v is None else str(v)) for k, v in (fields[secret] or {}).items()}
        elif secret == "headers" and moved:
            raise ValueError("the url now points at another host: enter the header values again")
    try:
        cfg = ServerConfig(**{**merged, "name": name})
    except (ValidationError, ValueError, TypeError) as e:
        raise ValueError(problem(e)) from None
    data["servers"][name] = cfg.model_dump(exclude={"name"}, exclude_defaults=True) | {"enabled": cfg.enabled}
    _write_file(data)
    return cfg


def remove_server(name: str) -> None:
    if name in BUNDLED:
        raise ValueError("a bundled server cannot be removed, only switched off")
    data = _read_file()
    if name not in data["servers"]:
        raise KeyError(name)
    del data["servers"][name]
    _write_file(data)


def load_config() -> tuple[dict[str, ServerConfig], str | None]:
    """The defaults merged with mcp.json, plus an error text when something in it is invalid."""
    servers = default_servers()
    path = config.mcp_file()
    if not path.exists():
        return servers, None
    try:
        data = json.loads(path.read_text())
        entries = data["servers"]
        if not isinstance(entries, dict):
            raise ValueError("'servers' must be an object")
    except (OSError, ValueError, KeyError, TypeError) as e:
        return servers, f"{path.name}: {type(e).__name__}: {e}"
    errors: list[str] = []
    for name, fields in entries.items():
        try:
            if not isinstance(fields, dict):
                raise ValueError("an entry must be an object")
            if name in BUNDLED:   # only on/off and its environment may change
                keep = {k: fields[k] for k in ("enabled", "env") if k in fields}
                servers[name] = servers[name].model_copy(update=ServerConfig(
                    **{**servers[name].model_dump(), **keep}).model_dump())
            else:
                servers[name] = ServerConfig(**{**fields, "name": name})
        except (ValidationError, ValueError, TypeError) as e:
            errors.append(f"{name}: {str(e).splitlines()[0][:200]}")
    return servers, ("; ".join(errors) if errors else None)


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}_{tool}")[:64]


def _walk(value, key: str | None = None, depth: int = 0):
    """Every (key, string) in nested dicts and lists, to WALK_DEPTH levels."""
    if isinstance(value, str):
        yield key, value
    elif depth < WALK_DEPTH:
        if isinstance(value, dict):
            for k, v in value.items():
                yield from _walk(v, k if isinstance(k, str) else None, depth + 1)
        elif isinstance(value, (list, tuple)):
            for v in value:
                yield from _walk(v, key, depth + 1)


async def generic_prepare(risk: Risk, ctx: ToolContext, args: dict) -> Prepared:
    """What a call to an unknown tool touches, read off its arguments: paths under the usual
    path keys, the env file wherever it is named, and every URL. No lookup, nothing touched."""
    mode = "read" if risk == "read" else "write"
    paths: dict[PathUse, None] = {}
    hosts: dict[HostUse, None] = {}
    urls: list[str] = []
    for key, text in _walk(args):
        if text.startswith(("http://", "https://")):
            hosts[resolve.host_of_url(text)] = None
            urls.append(text)
            continue
        if key in PATH_KEYS:
            paths[PathUse(str(resolve.resolve_path(text, ctx.workdir)), mode)] = None
            continue
        if len(text) <= 4096 and text.strip():
            try:
                p = resolve.resolve_path(text.strip(), ctx.workdir)
            except ToolError:
                continue    # cannot be a path
            if resolve.protected(p, "read"):
                paths[PathUse(str(p), "read")] = None
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()
    return Prepared(paths=tuple(paths), hosts=tuple(hosts),
                    url_path=resolve.url_path(urls[0]) if len(urls) == 1 else None,
                    grant_target=digest[:16], resolve_hosts=bool(hosts))


def resolved_args(args: dict, workdir: Path) -> dict:
    """A copy of the arguments with every path-key string replaced by its resolved path, so
    the path that was checked and shown is the path the server opens."""
    def fix(value, key=None, depth=0):
        if isinstance(value, str):
            return str(resolve.resolve_path(value, workdir)) if key in PATH_KEYS else value
        if depth >= WALK_DEPTH:
            return value
        if isinstance(value, dict):
            return {k: fix(v, k if isinstance(k, str) else None, depth + 1)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v, key, depth + 1) for v in value]
        return value
    return fix(copy.deepcopy(args))


# The SDK ends a server by closing its stdin and only kills the process group when the
# leader does not exit by itself, so children of a server that exits cleanly (or crashes)
# would live on. The server is therefore started through this one-line launcher, which
# records its pid (the SDK makes it a session leader, so that pid is the group id) and then
# becomes the server; the manager kills whatever is left of the group afterwards.
_LAUNCHER = ("import os,sys;f=open(sys.argv[1],'w');f.write(str(os.getpid()));f.close();"
             "os.execvp(sys.argv[2],sys.argv[2:])")


def _kill_group(pidfile: Path) -> None:
    try:
        pgid = int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return
    finally:
        pidfile.unlink(missing_ok=True)
    if pgid > 1:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pidfile: Path) -> bool:
    """Whether the process a stdio server was started as is still there. True when that cannot be told."""
    try:
        os.kill(int(pidfile.read_text().strip()), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        return True
    return True


class _Gone(Exception):
    """The session ended under a call. The task that owns the server has been told to start it again."""


class ServerStatus(BaseModel):
    name: str
    transport: str
    state: Literal["disabled", "starting", "ready", "restarting", "dead"]
    tools: list[str] = []
    held: list[str] = []        # tools the scanner keeps from the model until the owner approves them
    error: str | None = None
    restarts: int = 0
    forced_restarts: int = 0


class _Server:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.state = "starting" if cfg.enabled else "disabled"
        self.tools: list[str] = []
        self.scan: dict | None = None     # the scanner's last report on what this server sent
        self.error: str | None = None
        self.restarts = 0
        self.forced = 0
        self.ended: list[float] = []      # when it stopped after it had come up, within RESTART_WINDOW
        self.failed_starts = 0            # in a row
        self.session: ClientSession | None = None
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()       # stop, forced restart, or a broken session
        self.settled = asyncio.Event()    # ready or dead (or disabled)
        self.force = False
        if not cfg.enabled:
            self.settled.set()


_ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]{1,63})\}")


def resolve_headers(headers: dict[str, str]) -> dict[str, str]:
    """The headers as they are sent: ${NAME} in a value stands for that key of the .env file,
    so that a key is kept in one place. The owner writes headers; no inspected code can."""
    def value_of(name: str, header: str) -> str:
        got = config.env(name)
        if not got:
            raise ValueError(f"header {header} names ${{{name}}}, which is not set in the .env file")
        return got
    return {k: _ENV_REF.sub(lambda m, k=k: value_of(m.group(1), k), v) for k, v in headers.items()}


def explain(e: BaseException, answered: int | None = None) -> str:
    """Why a server did not start, in words: the error inside a task group, and what a
    refused sign-in asks of the owner. `answered` is the last HTTP status an http server
    gave, for the errors that do not carry it."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    status = getattr(getattr(e, "response", None), "status_code", None)
    if not isinstance(status, int) and answered is not None and answered >= 400:
        status = answered
    if status in (401, 403):
        return (f"the server answered {status}: it wants to know who is calling. Edit the server and add its key as a header, "
                "for example Authorization with the value Bearer <key>, or Bearer ${NAME} for a key kept in the .env file. "
                "A server that only takes an OAuth sign-in in a browser cannot be used yet.")
    if isinstance(status, int):
        return f"the server answered {status}"
    if isinstance(e, ValueError) and ".env file" in str(e):
        return str(e)[:300]
    return f"{type(e).__name__}: {str(e)[:300]}"


@asynccontextmanager
async def _http(cfg: ServerConfig, seen: dict | None = None):
    """The http transport with the owner's headers on every request. No proxy settings from
    the environment: the key goes to the configured host and nowhere else. `seen["status"]`
    is the status of the last answer, for a start that fails without saying why."""
    import httpx2

    async def note(response) -> None:
        if seen is not None:
            seen["status"] = response.status_code
    async with httpx2.AsyncClient(headers=resolve_headers(cfg.headers), timeout=httpx2.Timeout(30.0, read=300.0), trust_env=False,
                                  event_hooks={"response": [note]}) as client:
        async with streamable_http_client(cfg.url, http_client=client) as streams:
            yield streams


def _held(srv: _Server) -> list[str]:
    return [t["name"] for t in (srv.scan or {}).get("tools", []) if t["held"]]


def _text(result) -> ToolResult:
    parts = []
    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", "unknown")
        parts.append(item.text if kind == "text" else f"[{kind} content omitted]")
    is_error = bool(getattr(result, "is_error", None) or getattr(result, "isError", False))
    return ToolResult("\n".join(parts), is_error=is_error)


class Manager:
    def __init__(self, configs: dict[str, ServerConfig] | None = None):
        self._servers = {n: _Server(c) for n, c in (configs or {}).items()}
        self._stopping = False
        self._replacing = asyncio.Lock()   # one replace at a time: two would each start a server

    # ---- lifecycle

    async def start(self, wait: float | None = None) -> None:
        """Start every enabled server. Returns when all are ready or dead, or after `wait`
        seconds; a slow server keeps starting in the background."""
        wait = INIT_TIMEOUT if wait is None else wait
        for srv in self._servers.values():
            if srv.cfg.enabled and srv.task is None:
                srv.task = asyncio.create_task(self._own(srv))
        pending = [asyncio.ensure_future(s.settled.wait()) for s in self._servers.values()]
        if pending:
            _, late = await asyncio.wait(pending, timeout=wait)
            for f in late:
                f.cancel()

    async def replace(self, cfg: ServerConfig, wait: float | None = None) -> None:
        """Stop the server of this name if there is one and start it again with `cfg`
        (or leave it off). No other server is touched."""
        async with self._replacing:
            old = self._servers.get(cfg.name)
            if old is not None and old.task is not None:
                old.task.cancel()  # leaving the transport unregisters its tools and kills its group
                await asyncio.gather(old.task, return_exceptions=True)
            srv = self._servers[cfg.name] = _Server(cfg)
            if cfg.enabled and not self._stopping:
                srv.task = asyncio.create_task(self._own(srv))
                try:
                    await asyncio.wait_for(srv.settled.wait(), INIT_TIMEOUT if wait is None else wait)
                except asyncio.TimeoutError:
                    pass           # a slow server keeps starting in the background

    async def stop(self) -> None:
        self._stopping = True
        tasks = [s.task for s in self._servers.values() if s.task is not None]
        for srv in self._servers.values():
            srv.wake.set()
        if tasks:
            _, slow = await asyncio.wait(tasks, timeout=0.5)
            for t in slow:      # still starting, or stuck: leaving the transport kills the group
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _transport(self, cfg: ServerConfig, errlog, seen: dict | None = None):
        if cfg.transport == "http":
            return _http(cfg, seen)
        return stdio_client(StdioServerParameters(
            command=sys.executable,
            args=["-c", _LAUNCHER, str(self._pidfile(cfg)), cfg.command, *cfg.args],
            env=cfg.env or None, cwd=cfg.cwd), errlog=errlog)

    @staticmethod
    def _pidfile(cfg: ServerConfig) -> Path:
        return config.proc_dir() / f"mcp-{cfg.name}-{os.getpid()}.pid"

    async def _own(self, srv: _Server) -> None:
        cfg, source = srv.cfg, f"mcp:{srv.cfg.name}"
        while not self._stopping:
            srv.wake.clear()
            srv.force = False
            seen: dict = {}
            came_up = False
            try:
                async with AsyncExitStack() as stack:
                    errlog = stack.enter_context(
                        open(config.logs_dir() / f"mcp-{cfg.name}.log", "a", encoding="utf-8"))
                    streams = await stack.enter_async_context(self._transport(cfg, errlog, seen))
                    session = await stack.enter_async_context(
                        ClientSession(streams[0], streams[1]))
                    await session.initialize()
                    skipped = await self._register(srv, session)
                    srv.session, srv.state = session, "ready"
                    came_up, srv.failed_starts = True, 0
                    notes = [("skipped tools: " + ", ".join(skipped))] if skipped else []
                    held = _held(srv)
                    if held:
                        notes.append(f"{len(held)} tool(s) held for review by the scanner: " + ", ".join(held)[:300])
                    srv.error = "; ".join(notes) or None
                    srv.settled.set()
                    await self._until_it_ends(srv)
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 - a broken server must not end the app
                srv.error = explain(e, seen.get("status"))
            finally:
                srv.session, srv.tools = None, []
                registry.unregister_source(source)
                if cfg.transport == "stdio":
                    _kill_group(self._pidfile(cfg))
            if self._stopping:
                break
            if srv.force:
                if srv.forced > MAX_FORCED_RESTARTS:
                    break
            elif not came_up:
                if srv.failed_starts >= MAX_FAILED_STARTS:
                    break
                srv.failed_starts += 1
                srv.restarts += 1
                srv.error = srv.error or "the server stopped"
            else:
                # A server that worked and stopped is started again every time. One that keeps stopping is left alone:
                # starting it over and over would only hide that it does not work.
                now = time.monotonic()
                srv.ended = [t for t in srv.ended if now - t < RESTART_WINDOW] + [now]
                srv.error = srv.error or "the server stopped"
                if len(srv.ended) > MAX_RESTARTS:
                    srv.error = (f"it stopped {len(srv.ended)} times within {RESTART_WINDOW / 60:.0f} minutes and was not "
                                 f"started again; the last time: {srv.error}")
                    break
                srv.restarts += 1
            srv.state = "restarting"
            srv.settled.clear()
        if not self._stopping:
            srv.state = "dead"
            srv.error = srv.error or "the server stopped"
        srv.settled.set()

    async def _until_it_ends(self, srv: _Server) -> None:
        """Returns when the server has to be ended: a stop, a forced restart, a session that broke under a call.
        The process of a stdio server is looked at every ALIVE_EVERY seconds as well: it can end while nobody
        calls it, and nothing else says so before the next call fails."""
        if srv.cfg.transport != "stdio":
            await srv.wake.wait()
            return
        pidfile = self._pidfile(srv.cfg)
        while not srv.wake.is_set():
            try:
                await asyncio.wait_for(srv.wake.wait(), ALIVE_EVERY)
            except TimeoutError:
                if not _alive(pidfile):
                    srv.error = ENDED_IDLE
                    return

    async def _register(self, srv: _Server, session: ClientSession) -> list[str]:
        cfg, skipped, cursor, pages = srv.cfg, [], None, 0
        listed = []
        while True:
            page = await (session.list_tools(params={"cursor": cursor}) if cursor
                          else session.list_tools())
            listed += list(page.tools)
            cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
            pages += 1
            if not cursor:
                break
            if pages >= MAX_TOOL_PAGES or len(listed) >= MAX_TOOLS:
                raise ValueError(f"the server lists more than {MAX_TOOLS} tools or {MAX_TOOL_PAGES} pages of them")
        schemas = {}
        for t in listed:
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None)
            schemas[t.name] = schema if isinstance(schema, dict) and schema.get("type") == "object" else {"type": "object"}
        # What the server says goes into every request: read it before the model does.
        report = mcp_scan.judge(
            cfg.name, mcp_scan.launch_digest(cfg),
            [{"name": t.name, "description": t.description or "", "schema": schemas[t.name]} for t in listed],
            pins=mcp_scan.load_pins(), exposed=cfg.expose,
            others={x.name for x in registry.all() if x.source != f"mcp:{cfg.name}"})
        record = report.pop("pins")
        if report["first_sight"] and record["tools"]:
            mcp_scan.save_pin(cfg.name, record)
        srv.scan = report
        held = set(_held(srv))
        for t in listed:
            if t.name in held:
                continue
            schema = schemas[t.name]

            async def handler(ctx: ToolContext, args: dict, _tool=t.name) -> ToolResult:
                return await self.call_tool(cfg.name, _tool, resolved_args(args, ctx.workdir))

            async def prepare(ctx: ToolContext, args: dict) -> Prepared:
                return await generic_prepare(cfg.risk, ctx, args)

            name = tool_name(cfg.name, t.name)
            try:
                registry.register(Tool(name=name, description=t.description or "",
                                       parameters=schema, risk=cfg.risk, handler=handler,
                                       prepare=prepare, source=f"mcp:{cfg.name}",
                                       expose=cfg.expose))
                srv.tools.append(name)
            except ValueError:
                skipped.append(t.name)
        return skipped

    async def remove(self, name: str) -> None:
        """Stop a server and forget it (its tools are unregistered with it)."""
        async with self._replacing:
            old = self._servers.pop(name, None)
            if old is not None and old.task is not None:
                old.task.cancel()
                await asyncio.gather(old.task, return_exceptions=True)

    async def ping(self, name: str, timeout: float = 10.0) -> float | None:
        """Milliseconds a ping took, or None when the server is not ready or did not answer."""
        srv = self._servers.get(name)
        if srv is None or srv.session is None or srv.state != "ready":
            return None
        began = time.monotonic()
        try:
            await asyncio.wait_for(srv.session.send_ping(), timeout)
        except Exception:  # noqa: BLE001 - any failure is "no answer"
            return None
        return round((time.monotonic() - began) * 1000, 1)

    def scan(self, name: str) -> dict | None:
        """The scanner's report on what the server sent when it last started, or None."""
        srv = self._servers.get(name)
        return srv.scan if srv is not None else None

    async def approve(self, name: str, approved: dict[str, str]) -> None:
        """The owner accepts held tool definitions, each by the digest that was shown.
        The server is started again so that they are registered."""
        srv = self._servers.get(name)
        if srv is None or srv.scan is None:
            raise KeyError(name)
        report = srv.scan
        current = (mcp_scan.load_pins() or {}).get(name)
        if not isinstance(current, dict) or current.get("launch") != report["launch"]:
            current = {"launch": report["launch"], "tools": {}}
        mcp_scan.save_pin(name, mcp_scan.with_approved(current, report, approved))
        await self.replace(srv.cfg)

    # ---- use

    def status(self) -> list[ServerStatus]:
        return [ServerStatus(name=s.cfg.name, transport=s.cfg.transport, state=s.state,
                             tools=list(s.tools), held=_held(s) if s.state == "ready" else [],
                             error=s.error, restarts=s.restarts,
                             forced_restarts=s.forced) for s in self._servers.values()]

    def available(self, server: str) -> bool:
        srv = self._servers.get(server)
        return srv is not None and srv.state == "ready" and srv.session is not None

    def _force_restart(self, srv: _Server) -> None:
        """The server is still busy in a tool: end it (its process group dies with the
        transport) and start it again."""
        if srv.session is None or srv.force:
            return
        srv.forced += 1
        srv.force = True
        srv.session = None
        srv.state = "restarting"
        srv.wake.set()

    @staticmethod
    def _broken(srv: _Server, session: ClientSession | None, why: str) -> None:
        """The session is gone (child exit, EOF, transport error): have the task that owns the server start it
        again. Nothing when that was said already."""
        if session is not None and srv.session is session:
            srv.session = None
            srv.state = "restarting"
            srv.error = why
            srv.settled.clear()
            srv.wake.set()

    async def _back(self, srv: _Server) -> bool:
        """Whether the server is ready again after it was found gone. Waits as long as a start may take."""
        try:
            await asyncio.wait_for(srv.settled.wait(), INIT_TIMEOUT)
        except TimeoutError:
            return False
        return self.available(srv.cfg.name)

    async def call_tool(self, server: str, tool: str, arguments: dict,
                        timeout: float = CALL_TIMEOUT) -> ToolResult:
        """One call of a tool of this server. A stdio server whose process ended while nobody called it is
        started again first: the call is not lost to it. A server that stops under the call is started again
        too, and the call is made once more only when the server is one that only reads (risk "read"): any
        other may have done part of its work."""
        srv = self._servers.get(server)
        if (srv is not None and self.available(server) and srv.cfg.transport == "stdio"
                and not _alive(self._pidfile(srv.cfg))):
            self._broken(srv, srv.session, ENDED_IDLE)
            await self._back(srv)
        if srv is None or not self.available(server):
            why = (srv.error or "")[:300] if srv is not None else ""
            raise ToolError(f"MCP server {server} is not available" + (f": {why}" if why else ""))
        try:
            return await self._call(srv, tool, arguments, timeout)
        except _Gone:
            pass
        printed = f"What the server printed is in {config.logs_dir() / f'mcp-{server}.log'}."
        if srv.cfg.risk != "read":
            raise ToolError(f"MCP server {server} stopped while it ran this call. It is being started again. The call was "
                            f"not repeated: it may have done part of its work. Call it again if that is safe. {printed}")
        if not (await self._back(srv) and tool_name(server, tool) in srv.tools):
            raise ToolError(f"MCP server {server} stopped while it ran this call, and the call could not be made again: "
                            f"the server did not come up, or no longer offers this tool. {printed}")
        try:
            return await self._call(srv, tool, arguments, timeout)
        except _Gone:
            raise ToolError(f"MCP server {server} stopped twice while it ran this call: once more after it was started "
                            f"again. The call seems to be what stops it. {printed}") from None

    async def _call(self, srv: _Server, tool: str, arguments: dict, timeout: float) -> ToolResult:
        """One call on the session the server has now. _Gone when the session ends under it."""
        server, session = srv.cfg.name, srv.session
        fut = asyncio.ensure_future(session.call_tool(tool, arguments))
        try:
            done, _ = await asyncio.wait({fut}, timeout=timeout)
        except asyncio.CancelledError:
            async def watch() -> None:   # cancelled by the caller: is the server still busy?
                await asyncio.wait({fut}, timeout=CANCEL_GRACE)
                if not fut.done():
                    fut.cancel()
                    if srv.session is session:
                        self._force_restart(srv)
                elif not fut.cancelled():
                    fut.exception()      # retrieved, so it is not reported as never read
            srv_task = asyncio.ensure_future(watch())
            srv_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            raise
        if not done:
            fut.cancel()
            if srv.session is session:
                self._force_restart(srv)
            raise ToolError(f"MCP server {server} timed out after {timeout:g} s")
        try:
            return _text(fut.result())
        except asyncio.CancelledError:
            raise ToolError(f"MCP server {server} stopped") from None
        except MCPError as e:
            if e.code != CONNECTION_CLOSED:
                # The server answered, with an error of its own in place of a result: it runs, and is left running.
                return ToolResult(f"The server answered with an error instead of a result: {str(e.message)[:2000]} "
                                  f"(code {e.code})", is_error=True)
            self._broken(srv, session, f"{type(e).__name__}: {str(e)[:300]}")
            raise _Gone from None
        except Exception as e:
            self._broken(srv, session, f"{type(e).__name__}: {str(e)[:300]}")
            raise _Gone from None


manager: Manager | None = None


async def start(wait: float | None = None) -> Manager:
    """Start the configured servers (the defaults merged with mcp.json)."""
    global manager
    servers, error = load_config()
    manager = Manager(servers)
    await manager.start(wait)
    if error:
        for s in manager._servers.values():   # noqa: SLF001 - surface the config error
            s.error = s.error or error
            break
    return manager


async def stop() -> None:
    global manager
    if manager is not None:
        await manager.stop()
        manager = None


def get() -> Manager | None:
    return manager
