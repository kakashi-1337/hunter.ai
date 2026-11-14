"""Search tools: web_search behind a small provider interface (self-hosted SearXNG by
default), and repo_search on local ripgrep."""
from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from .. import config, procs
from . import resolve
from .base import PathUse, Prepared, Tool, ToolContext, ToolError, ToolResult

RG_TIMEOUT = 30.0
DEFAULT_LIMIT = 8
DEFAULT_MAX_RESULTS = 100


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    engine: str = ""


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int, page: int = 1) -> list[SearchResult]: ...


class SearxngProvider:
    name = "searxng"

    def __init__(self, base_url: str | None = None):
        self.base_url = base_url

    async def search(self, query: str, *, limit: int, page: int = 1) -> list[SearchResult]:
        base = (self.base_url or config.searxng_url()).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                r = await client.get(f"{base}/search", params={
                    "q": query, "format": "json", "pageno": page, "safesearch": 0})
        except httpx.HTTPError:
            raise ToolError(f"SearXNG is not running at {base}. Start it with "
                            "scripts/searxng.sh start.") from None
        if r.status_code == 403:
            raise ToolError("SearXNG refused the JSON format. Run scripts/searxng.sh start to "
                            "write its settings.")
        if r.status_code != 200:
            raise ToolError(f"SearXNG answered HTTP {r.status_code}")
        try:
            items = r.json().get("results") or []
        except ValueError:
            raise ToolError("SearXNG sent a response that is not JSON") from None
        out = [SearchResult(title=str(i.get("title") or ""), url=str(i.get("url") or ""),
                            snippet=str(i.get("content") or ""), engine=str(i.get("engine") or ""))
               for i in items if isinstance(i, dict) and i.get("url")]
        return out[:limit]


_provider: SearchProvider | None = None


def provider() -> SearchProvider:
    return _provider or SearxngProvider()


def set_provider(p: SearchProvider | None) -> None:
    global _provider
    _provider = p


async def _prepare_web_search(ctx: ToolContext, args: dict) -> Prepared:
    # The query goes to public search engines, not to the target: scope does not apply.
    return Prepared(grant_target="search")


async def _web_search(ctx: ToolContext, args: dict) -> ToolResult:
    results = await provider().search(args["query"], limit=int(args.get("limit") or DEFAULT_LIMIT),
                                      page=int(args.get("page") or 1))
    if not results:
        return ToolResult("no results")
    lines: list[str] = []
    for n, r in enumerate(results, 1):
        lines += [f"{n}. {r.title}", f"   {r.url}", f"   {' '.join(r.snippet.split())[:300]}"]
    return ToolResult("\n".join(lines))


# ---------------------------------------------------------------- repo_search

async def _prepare_repo_search(ctx: ToolContext, args: dict) -> Prepared:
    root = resolve.resolve_path(args.get("path") or ".", ctx.workdir)
    if not root.exists():
        raise ToolError(f"no such path: {root}")
    return Prepared(paths=(PathUse(str(root), "read"),), grant_target=str(root))


def _excludes(directory: Path) -> list[str]:
    """Files below the root that are never searched, whatever glob the model sends. They come
    after the model's glob because ripgrep lets a later glob win."""
    out: list[str] = []
    for name in resolve.SENSITIVE_NAMES:
        out += ["--glob", "!" + name]
    for folder in resolve.sensitive_dirs():
        if resolve.is_inside(folder, directory) and Path(resolve._real(folder)) != directory:  # noqa: SLF001
            out += ["--glob", "!/" + os.path.relpath(resolve._real(folder), directory) + "/**"]  # noqa: SLF001
    for f in (config.env_file(), config.mcp_file()):
        if resolve.is_inside(f, directory):
            out += ["--glob", "!/" + os.path.relpath(resolve._real(f), directory)]  # noqa: SLF001
    return out


def _kill(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _repo_search(ctx: ToolContext, args: dict) -> ToolResult:
    root = Path(ctx.prepared.paths[0].path)
    directory, target = (root, ".") if root.is_dir() else (root.parent, root.name)
    limit = int(args.get("max_results") or DEFAULT_MAX_RESULTS)
    # The model's values are only ever the argument that follows a fixed flag.
    argv = ["rg", "--no-config", "--line-number", "--no-heading", "--color", "never",
            "--max-columns", "300", "--max-columns-preview", "--max-filesize", "5M"]
    if args.get("fixed_strings"):
        argv.append("--fixed-strings")
    if args.get("ignore_case"):
        argv.append("--ignore-case")
    if args.get("context"):
        argv += ["--context", str(int(args["context"]))]
    if args.get("glob"):
        argv += ["--glob", args["glob"]]
    argv += _excludes(directory) + ["--regexp", args["pattern"], "--", target]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, cwd=str(directory), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True, env=procs.child_env())
    except FileNotFoundError:
        raise ToolError("ripgrep (rg) is not installed") from None
    lines: list[str] = []
    matches, cut = 0, False
    try:
        async with asyncio.timeout(RG_TIMEOUT):
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                lines.append(line)
                if re.match(r"^(?:[^:\n]*:)?\d+:", line):
                    matches += 1
                    if matches >= limit:
                        cut = True
                        break
            if cut:
                _kill(process)
            stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
            code = await process.wait()
    except TimeoutError:
        _kill(process)
        await process.wait()
        raise ToolError(f"search timed out after {RG_TIMEOUT:g} s; narrow the path or pattern") \
            from None
    except asyncio.CancelledError:
        _kill(process)
        raise
    head = f"root: {root}"
    if not cut and code == 2:
        raise ToolError(f"ripgrep: {stderr.strip()[:300]}")
    if not lines:
        return ToolResult(f"{head}\nno matches")
    if cut:
        lines.append(f"[stopped at {limit} matching lines; narrow the search or raise max_results]")
    return ToolResult("\n".join([head, *lines]))


def tools() -> list[Tool]:
    return [
        Tool("web_search", "Search the web. Returns titles, URLs, and snippets.",
             {"type": "object", "properties": {
                 "query": {"type": "string", "minLength": 1, "maxLength": 400},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                 "page": {"type": "integer", "minimum": 1, "maximum": 10}},
              "required": ["query"], "additionalProperties": False},
             "network", _web_search, prepare=_prepare_web_search),
        Tool("repo_search", (
            "Search files under a folder (or one file) with ripgrep. Returns "
            "path:line:text lines. Files that usually hold credentials are never searched."),
            {"type": "object", "properties": {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 500},
                "path": {"type": "string"}, "glob": {"type": "string", "maxLength": 200},
                "fixed_strings": {"type": "boolean"}, "ignore_case": {"type": "boolean"},
                "context": {"type": "integer", "minimum": 0, "maximum": 5},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500}},
             "required": ["pattern"], "additionalProperties": False},
            "read", _repo_search, prepare=_prepare_repo_search),
    ]
