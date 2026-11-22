"""A small stdio MCP server for tests. Never the owner's real servers.

    python fake_mcp_server.py [--hang]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("fake")
CALLS: dict[str, list[dict]] = {}


def _note(tool: str, **args) -> None:
    CALLS.setdefault(tool, []).append(args)


@mcp.tool()
def echo(text: str) -> str:
    _note("echo", text=text)
    return text


@mcp.tool()
def fail() -> str:
    raise ToolError("boom")


@mcp.tool()
def die() -> str:
    os._exit(1)


if not Path(os.environ.get("FAKE_WITHOUT_DIE_ONCE_IF") or "/nonexistent").exists():
    @mcp.tool()
    def die_once(marker: str) -> str:
        """Ends the server the first time it is called with this marker file, and answers from then on."""
        if Path(marker).exists():
            return "survived"
        Path(marker).write_text("x")
        os._exit(1)


@mcp.tool()
def refuse() -> str:
    """Answers with an error of the protocol in place of a result, and goes on running."""
    raise MCPError(-32603, "the index is not built yet")


@mcp.tool()
def env_names() -> str:
    return "\n".join(sorted(os.environ))


@mcp.tool()
def slow(seconds: float) -> str:
    time.sleep(seconds)
    return "done"


@mcp.tool()
def spawn_child() -> str:
    child = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return str(child.pid)


@mcp.tool()
def echo_path(path: str) -> str:
    _note("echo_path", path=path)
    return path


@mcp.tool()
def last_calls(tool: str) -> str:
    return json.dumps(CALLS.get(tool, []))


# ---- chunker-shaped tools: the real server's parameter names and output layout

def _lines(path: str) -> tuple[Path, list[str]]:
    p = Path(path)
    if not p.is_file():
        raise ToolError(f"not a file: {p}")
    return p, p.read_text(errors="replace").splitlines()


def _numbered(lines: list[str], first: int, number: bool) -> str:
    if not number:
        return "\n".join(lines)
    width = len(str(first + len(lines) - 1))
    return "\n".join(f"{first + i:>{width}}\t{ln}" for i, ln in enumerate(lines))


def _chunks(lines: list[str], size: int) -> list[tuple[int, int]]:
    """Pack whole lines into `size` characters: (first line index, end index) per chunk."""
    out, start, used = [], 0, 0
    for i, ln in enumerate(lines):
        if used and used + len(ln) + 1 > size:
            out.append((start, i))
            start, used = i, 0
        used += len(ln) + 1
    if lines:
        out.append((start, len(lines)))
    return out


@mcp.tool()
def file_info(path: str) -> str:
    _note("file_info", path=path)
    p, lines = _lines(path)
    return "\n".join([f"path: {p}", f"size: {p.stat().st_size:,} bytes", "type: text (utf-8)",
                      f"lines: {len(lines):,} (longest line: "
                      f"{max(map(len, lines), default=0):,} chars)"])


@mcp.tool()
def read_chunk(path: str, index: int, mode: str = "auto", chunk_size: int = 0, overlap: int = 0,
               number_lines: bool = True) -> str:
    _note("read_chunk", path=path, index=index, mode=mode, chunk_size=chunk_size,
          number_lines=number_lines)
    p, lines = _lines(path)
    if not lines:
        return f"{p.name} is empty (0 bytes)."
    size = chunk_size or 8000
    chunks = _chunks(lines, size)
    if index > len(chunks):
        raise ToolError(f"chunk {index} does not exist: {p.name} has {len(chunks)} chunk(s) "
                        f"with mode=chars chunk_size={size} overlap=0")
    a, b = chunks[index - 1]
    body = _numbered(lines[a:b], a + 1, number_lines)
    head = (f"=== {p.name} | chunk {index}/{len(chunks)} | lines {a + 1}-{b} of {len(lines):,} | "
            f"{len(body):,} chars | mode=chars chunk_size={size} overlap=0 ===")
    foot = f"=== end of {p.name} ===" if index == len(chunks) \
        else f'=== next: read_chunk("{p}", {index + 1}) ==='
    return f"{head}\n{body}\n{foot}"


@mcp.tool()
def read_lines(path: str, start_line: int, end_line: int = 0, number_lines: bool = True) -> str:
    _note("read_lines", path=path, start_line=start_line, end_line=end_line,
          number_lines=number_lines)
    p, lines = _lines(path)
    if start_line > len(lines):
        raise ToolError(f"start_line {start_line} is past the end: {p.name} has "
                        f"{len(lines):,} lines")
    end = min(end_line or start_line + 199, len(lines))
    body = _numbered(lines[start_line - 1:end], start_line, number_lines)
    head = f"=== {p.name} | lines {start_line}-{end} of {len(lines):,} | {len(body):,} chars ==="
    foot = "=== end of file ===" if end == len(lines) \
        else f'=== next: read_lines("{p}", {end + 1}) ==='
    return f"{head}\n{body}\n{foot}"


@mcp.tool()
def search_file(path: str, pattern: str, regex: bool = True, ignore_case: bool = False,
                max_hits: int = 50, context: int = 0, after_line: int = 0) -> str:
    _note("search_file", path=path, pattern=pattern, regex=regex, ignore_case=ignore_case,
          max_hits=max_hits, context=context, after_line=after_line)
    p, lines = _lines(path)
    if pattern == "HANG":
        time.sleep(300)
    try:
        pat = re.compile(pattern if regex else re.escape(pattern),
                         re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"bad regex: {e}") from None
    out = [f"{p.name}: /{pattern}/{'i' if ignore_case else ''} ({'regex' if regex else 'literal'})"]
    hits = [i for i, ln in enumerate(lines) if i >= after_line and pat.search(ln)]
    if not hits:
        return "\n".join(out + ["no matches" + (f" after line {after_line}" if after_line else "")])
    shown = hits[:max_hits]
    out.append(f"{len(shown)}{'+' if len(hits) > max_hits else ''} matching line(s); "
               "chunk numbers use mode=chars chunk_size=8000")
    out += [f"L{i + 1} (chunk 1): {lines[i][:300]}" for i in shown]
    if len(hits) > max_hits:
        out.append(f"... more hits; search_file(path, pattern, after_line={shown[-1] + 1}) "
                   "to continue")
    return "\n".join(out)


if __name__ == "__main__":
    if "--hang" in sys.argv:
        time.sleep(3600)
    mcp.run("stdio")
