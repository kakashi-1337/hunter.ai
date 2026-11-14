#!/usr/bin/env python3
"""
file_chunker_mcp.py - MCP server that lets an AI read big files chunk by chunk.

Tools
  file_info        size / lines / est. tokens / text-or-binary, longest line, suggested chunking
  chunk_file       build the chunk index for a file (by lines, chars, ~tokens, or bytes) -> manifest
  read_chunk       read chunk N (1-based). Uses the settings from the last chunk_file on that path,
                   or sensible defaults if chunk_file was never called.
  list_chunks      manifest with a one-line preview per chunk (paged)
  search_file      regex / plain search -> line numbers + which chunk each hit lives in
  read_lines       read an exact line range (jump straight to a search hit)
  split_to_files   physically write the chunks to disk as part files + a manifest.json

Works with the official `mcp` Python SDK, both v1 (FastMCP) and v2 (MCPServer).
Binary files (anything with NUL bytes) are chunked by bytes and returned as a hexdump.

Run
  python3 file_chunker_mcp.py                                   # stdio: Claude Desktop / Claude Code / pakkun
  python3 file_chunker_mcp.py --transport streamable-http --port 8765

Env
  CHUNKER_ROOTS=/dir1:/dir2   only allow files under these directories (default: anywhere)
  CHUNKER_MAX_MB=200          refuse files bigger than this (whole file is loaded into RAM)
"""
from __future__ import annotations

import argparse
import bisect
import functools
import json
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

try:  # mcp >= 2.0 (FastMCP was renamed to MCPServer)
    from mcp.server.mcpserver import MCPServer as _Server

    _SDK_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

    _SDK_V2 = False

try:
    from mcp.types import ToolAnnotations
except ImportError:  # very old 1.x without annotations
    ToolAnnotations = None  # type: ignore[assignment,misc]

try:  # a ToolError's message reaches the model; any other exception is masked as a generic crash in v2
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:  # pragma: no cover

        class ToolError(Exception):
            pass


# ---------------------------------------------------------------- config

Mode = Literal["auto", "lines", "chars", "tokens", "bytes"]

DEFAULT_SIZE = {"lines": 200, "chars": 8000, "tokens": 2000, "bytes": 2048}
CHARS_PER_TOKEN = 4  # rough estimate; good enough for context budgeting
LONG_LINE = 2000  # a longest line above this => prefer char chunking (minified js, one-line json)
MAX_BYTES = int(float(os.environ.get("CHUNKER_MAX_MB", "200")) * 1024 * 1024)
ROOTS: list[Path] = [
    Path(p).expanduser().resolve()
    for p in os.environ.get("CHUNKER_ROOTS", "").split(os.pathsep)
    if p.strip()
]

PREVIEW_CHARS = 80
HIT_LINE_CHARS = 240
MAX_LINES_PER_READ = 2000
FILE_CACHE_SIZE = 8
INDEX_CACHE_SIZE = 16


def _tok(n_chars: int) -> int:
    """Estimated token count for n characters (ceil)."""
    return -(-n_chars // CHARS_PER_TOKEN)


# ---------------------------------------------------------------- file loading


@dataclass
class Loaded:
    path: Path
    key: tuple  # (path, size, mtime_ns): changes whenever the file changes on disk
    size: int
    is_binary: bool
    encoding: str
    text: str = ""
    data: bytes = b""
    # bounds[i] = offset where 0-based line i starts; bounds[-1] = len(text)
    bounds: list[int] = field(default_factory=lambda: [0])
    _longest: int = -1

    @property
    def nlines(self) -> int:
        return len(self.bounds) - 1

    @property
    def length(self) -> int:
        return len(self.data) if self.is_binary else len(self.text)

    def line_of(self, offset: int) -> int:
        """0-based index of the line containing a text offset."""
        if offset >= len(self.text):
            return max(self.nlines - 1, 0)
        return bisect.bisect_right(self.bounds, offset) - 1

    def line_text(self, i: int) -> str:
        return self.text[self.bounds[i] : self.bounds[i + 1]].rstrip("\r\n")

    def longest_line(self) -> int:
        if self._longest < 0:
            self._longest = max(
                (max(b - a - 1, 0) for a, b in zip(self.bounds, self.bounds[1:])),
                default=0,
            )
        return self._longest


_FILES: "OrderedDict[tuple, Loaded]" = OrderedDict()


def _line_bounds(text: str) -> list[int]:
    starts = [0]
    i = text.find("\n")
    while i != -1:
        starts.append(i + 1)
        i = text.find("\n", i + 1)
    if starts[-1] == len(text):  # trailing newline (or empty file): no phantom empty last line
        starts.pop()
    return starts + [len(text)]


def _resolve(path: str) -> Path:
    """Expand ~, make absolute, and enforce CHUNKER_ROOTS. Does not require the path to exist."""
    if not path or not path.strip():
        raise ValueError("path is empty")
    p = Path(path.strip()).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    if ROOTS and not any(p == r or r in p.parents for r in ROOTS):
        allowed = ", ".join(str(r) for r in ROOTS)
        raise PermissionError(f"{p} is outside the allowed roots ({allowed})")
    return p


def _resolve_file(path: str) -> Path:
    p = _resolve(path)
    if not p.exists():
        raise FileNotFoundError(f"no such file: {p}")
    if not p.is_file():
        raise IsADirectoryError(f"not a regular file: {p}")
    return p


def _load(p: Path) -> Loaded:
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    hit = _FILES.get(key)
    if hit is not None:
        _FILES.move_to_end(key)
        return hit
    if st.st_size > MAX_BYTES:
        raise ValueError(
            f"{p.name} is {st.st_size / 1048576:.1f} MB; limit is {MAX_BYTES / 1048576:.0f} MB "
            f"(raise it with CHUNKER_MAX_MB)"
        )
    data = p.read_bytes()
    if b"\x00" in data[:8192]:
        loaded = Loaded(p, key, st.st_size, True, "binary", data=data)
    else:
        try:
            text, enc = data.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            text, enc = data.decode("latin-1"), "latin-1"
        loaded = Loaded(p, key, st.st_size, False, enc, text=text, bounds=_line_bounds(text))
    _FILES[key] = loaded
    while len(_FILES) > FILE_CACHE_SIZE:
        old_key, _ = _FILES.popitem(last=False)
        for ikey in [k for k in _INDEXES if k[0] == old_key]:
            del _INDEXES[ikey]
    return loaded


# ---------------------------------------------------------------- chunk index


@dataclass
class Chunk:
    index: int  # 1-based
    start: int  # char (text) or byte (binary) offset, inclusive
    end: int  # exclusive
    start_line: int  # 1-based; 0 for binary files
    end_line: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass
class Index:
    loaded: Loaded
    mode: str  # lines | chars | tokens | bytes
    size: int
    overlap: int
    chunks: list[Chunk]
    starts: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.starts = [c.start for c in self.chunks]

    @property
    def count(self) -> int:
        return len(self.chunks)

    def chunk_at(self, offset: int) -> int:
        """1-based index of the first chunk containing this offset (overlap-aware)."""
        k = bisect.bisect_right(self.starts, offset) - 1
        while k > 0 and self.chunks[k - 1].end > offset:
            k -= 1
        return self.chunks[max(k, 0)].index


_INDEXES: "OrderedDict[tuple, Index]" = OrderedDict()
_LAST: dict[str, tuple[str, int, int]] = {}  # path -> (mode, size, overlap) last used


def _chunk_lines(L: Loaded, size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    n, i = L.nlines, 0
    while i < n:
        j = min(i + size, n)
        chunks.append(Chunk(len(chunks) + 1, L.bounds[i], L.bounds[j], i + 1, j))
        if j >= n:
            break
        i = max(j - overlap, i + 1)
    return chunks


def _chunk_chars(L: Loaded, budget: int, overlap: int) -> list[Chunk]:
    """Pack whole lines into `budget`-char chunks; a single line longer than the
    budget is hard-cut (so one-line JSON / minified JS still chunks)."""
    chunks: list[Chunk] = []
    text_len, b, pos = len(L.text), L.bounds, 0
    while pos < text_len:
        end = min(pos + budget, text_len)
        if end < text_len:
            boundary = b[bisect.bisect_right(b, end) - 1]  # start of the line containing `end`
            if boundary > pos + budget // 2:  # cut at a line boundary unless a giant line forces a hard cut
                end = boundary
        chunks.append(Chunk(len(chunks) + 1, pos, end, L.line_of(pos) + 1, L.line_of(end - 1) + 1))
        if end >= text_len:
            break
        nxt = end - overlap
        if overlap:
            aligned = b[bisect.bisect_right(b, nxt) - 1]  # back up to a line start
            if aligned > pos:
                nxt = aligned
        pos = max(nxt, pos + 1)
    return chunks


def _chunk_bytes(L: Loaded, size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    n, pos = len(L.data), 0
    while pos < n:
        end = min(pos + size, n)
        chunks.append(Chunk(len(chunks) + 1, pos, end, 0, 0))
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return chunks


def _settings(L: Loaded, mode: str, chunk_size: int, overlap: int) -> tuple[str, int, int]:
    """Resolve auto/0 arguments against the last settings used for this path, or defaults."""
    last = _LAST.get(str(L.path))
    if last is not None and (last[0] == "bytes") != L.is_binary:
        last = None  # file changed type on disk; forget the old settings
    if mode == "auto" and chunk_size == 0 and overlap == 0 and last is not None:
        return last
    if mode == "auto":
        if last is not None:
            mode = last[0]
        elif L.is_binary:
            mode = "bytes"
        elif L.longest_line() > LONG_LINE:
            mode = "chars"
        else:
            mode = "lines"
    if L.is_binary and mode != "bytes":
        mode = "bytes"  # only thing that makes sense for a binary
    elif not L.is_binary and mode == "bytes":
        raise ValueError(f"mode='bytes' is for binary files; {L.path.name} is text. Use lines, chars or tokens.")
    size = chunk_size if chunk_size > 0 else DEFAULT_SIZE[mode]
    if overlap < 0 or overlap > size // 2:
        raise ValueError(f"overlap must be between 0 and chunk_size/2 (= {size // 2} for chunk_size={size})")
    return mode, size, overlap


def _get_index(p: Path, mode: str = "auto", chunk_size: int = 0, overlap: int = 0) -> Index:
    L = _load(p)
    mode, size, overlap = _settings(L, mode, chunk_size, overlap)
    key = (L.key, mode, size, overlap)
    idx = _INDEXES.get(key)
    if idx is None:
        if mode == "lines":
            chunks = _chunk_lines(L, size, overlap)
        elif mode == "bytes":
            chunks = _chunk_bytes(L, size, overlap)
        elif mode == "tokens":
            chunks = _chunk_chars(L, size * CHARS_PER_TOKEN, overlap * CHARS_PER_TOKEN)
        else:
            chunks = _chunk_chars(L, size, overlap)
        idx = Index(L, mode, size, overlap, chunks)
        _INDEXES[key] = idx
        while len(_INDEXES) > INDEX_CACHE_SIZE:
            _INDEXES.popitem(last=False)
    else:
        _INDEXES.move_to_end(key)
    _LAST[str(p)] = (mode, size, overlap)
    return idx


# ---------------------------------------------------------------- rendering


def _hexdump(data: bytes, base: int) -> str:
    rows = []
    for off in range(0, len(data), 16):
        row = data[off : off + 16]
        hx = " ".join(f"{x:02x}" for x in row)
        if len(row) > 8:
            hx = hx[:23] + " " + hx[23:]
        asc = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
        rows.append(f"{base + off:08x}  {hx:<48}  |{asc}|")
    return "\n".join(rows)


def _numbered(L: Loaded, start: int, end: int) -> str:
    seg = L.text[start:end]
    if not seg:
        return ""
    first = L.line_of(start) + 1
    width = len(str(L.line_of(end - 1) + 1))
    parts = seg.split("\n")
    if parts[-1] == "":  # segment ended with a newline
        parts.pop()
    return "\n".join(f"{first + i:>{width}}\t{ln}" for i, ln in enumerate(parts))


def _span(L: Loaded, c: Chunk) -> str:
    if L.is_binary:
        return f"bytes {c.start:#x}-{c.end:#x}"
    a = "" if c.start == 0 or L.text[c.start - 1] == "\n" else " (cont.)"
    z = "" if c.end == len(L.text) or L.text[c.end - 1] == "\n" else " (cut)"
    return f"lines {c.start_line}{a}-{c.end_line}{z}"


def _preview(L: Loaded, c: Chunk) -> str:
    if L.is_binary:
        return L.data[c.start : c.start + 16].hex(" ")
    for ln in L.text[c.start : c.end].splitlines():
        s = ln.strip()
        if s:
            return s[:PREVIEW_CHARS] + ("..." if len(s) > PREVIEW_CHARS else "")
    return "(blank)"


def _file_line(L: Loaded) -> str:
    if L.is_binary:
        return f"file: {L.path} (binary, {L.size:,} bytes)"
    return (
        f"file: {L.path} ({L.encoding}, {L.nlines:,} lines, {len(L.text):,} chars, "
        f"~{_tok(len(L.text)):,} tokens)"
    )


def _manifest(idx: Index, start: int, limit: int) -> str:
    L = idx.loaded
    out = [
        _file_line(L),
        f"chunking: mode={idx.mode} chunk_size={idx.size} overlap={idx.overlap} -> {idx.count} chunk(s)",
    ]
    if idx.count == 0:
        out.append("(file is empty)")
        return "\n".join(out)
    start = max(1, start)
    rows = idx.chunks[start - 1 : start - 1 + limit]
    for c in rows:
        if L.is_binary:
            size = f"{c.length:,} B"
        else:
            size = f"{c.length:,} ch ~{_tok(c.length):,} tok"
        out.append(f"#{c.index:<4} {_span(L, c):<28} {size:<24} | {_preview(L, c)}")
    last_shown = start - 1 + len(rows)
    if last_shown < idx.count:
        out.append(f"... {idx.count - last_shown} more; list_chunks(path, start={last_shown + 1}) to continue")
    out.append(f'next: read_chunk("{L.path}", 1)')
    return "\n".join(out)


# ---------------------------------------------------------------- MCP server

INSTRUCTIONS = """Read big files chunk by chunk instead of all at once.
Workflow: file_info(path) -> chunk_file(path) -> read_chunk(path, 1), read_chunk(path, 2), ... until the footer says end of file.
To jump around: search_file(path, pattern) gives the line and chunk of every hit; read_lines(path, start, end) reads an exact range.
Chunk settings (mode / chunk_size / overlap) stick per file once set, so later read_chunk calls can omit them.
Always pass absolute paths."""

mcp = _Server("file_chunker_mcp", instructions=INSTRUCTIONS)


def _ann(read_only: bool = True, idempotent: bool = True):
    if ToolAnnotations is None:
        return None
    return ToolAnnotations(
        readOnlyHint=read_only, destructiveHint=False, idempotentHint=idempotent, openWorldHint=False
    )


def _tool(name: str, read_only: bool = True):
    """Register a tool; expected failures (bad path, bad args, I/O) become ToolErrors whose
    message the model can read, instead of an opaque 'Error executing tool'."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, OSError) as e:
                raise ToolError(str(e)) from None

        return mcp.tool(name=name, annotations=_ann(read_only))(wrapper)

    return deco


PathArg = Annotated[str, Field(description="Absolute path to the file (~ is expanded)")]
ModeArg = Annotated[
    Mode,
    Field(
        description="How to split: 'lines' (chunk_size = lines), 'chars' (chunk_size = characters, "
        "line-aligned), 'tokens' (chunk_size = approx. tokens, line-aligned), 'bytes' (binary files, "
        "hexdump). 'auto' = reuse the settings last used for this file, or pick lines/chars/bytes by content."
    ),
]
SizeArg = Annotated[int, Field(ge=0, description="Chunk size in the unit of `mode`; 0 = default "
                                                  "(lines 200, chars 8000, tokens 2000, bytes 2048)")]
OverlapArg = Annotated[int, Field(ge=0, description="How much of the previous chunk to repeat at the start "
                                                     "of the next (same unit as mode), max chunk_size/2")]
NumberArg = Annotated[bool, Field(description="Prefix each line with its line number (text files)")]


@_tool("file_info")
def file_info(path: PathArg) -> str:
    """Inspect a file before reading it: size, text-or-binary, encoding, line count, longest line,
    estimated tokens, and the chunking this server will use by default. Cheap; call it first."""
    p = _resolve_file(path)
    L = _load(p)
    out = [f"path: {p}", f"size: {L.size:,} bytes"]
    if L.is_binary:
        out.append("type: binary (NUL bytes found) - read_chunk returns a hexdump, search_file matches raw bytes")
    else:
        out.append(f"type: text ({L.encoding})")
        out.append(f"lines: {L.nlines:,} (longest line: {L.longest_line():,} chars)")
        out.append(f"chars: {len(L.text):,} (~{_tok(len(L.text)):,} tokens)")
        if L.longest_line() > LONG_LINE:
            out.append("note: very long lines (minified / one-line data) - char or token chunking is the safe choice")
    idx = _get_index(p)
    if idx.count <= 1:
        out.append(f'small enough for one chunk: read_chunk("{p}", 1) returns the whole file')
    else:
        out.append(
            f"chunking: mode={idx.mode} chunk_size={idx.size} overlap={idx.overlap} -> {idx.count} chunks "
            f'(read_chunk("{p}", 1) .. {idx.count}; chunk_file to change)'
        )
    return "\n".join(out)


@_tool("chunk_file")
def chunk_file(path: PathArg, mode: ModeArg = "auto", chunk_size: SizeArg = 0, overlap: OverlapArg = 0) -> str:
    """Split a file into chunks and return the manifest (one row per chunk: index, line range,
    size, preview). The settings become the default for later read_chunk / list_chunks /
    search_file calls on the same path. Nothing is written to disk."""
    p = _resolve_file(path)
    idx = _get_index(p, mode, chunk_size, overlap)
    return _manifest(idx, 1, 200)


@_tool("list_chunks")
def list_chunks(
    path: PathArg,
    start: Annotated[int, Field(ge=1, description="First chunk index to show (1-based)")] = 1,
    limit: Annotated[int, Field(ge=1, le=500, description="How many rows to show")] = 100,
) -> str:
    """Show the chunk manifest of a file (paged) using its current chunk settings."""
    p = _resolve_file(path)
    idx = _get_index(p)
    return _manifest(idx, start, limit)


@_tool("read_chunk")
def read_chunk(
    path: PathArg,
    index: Annotated[int, Field(ge=1, description="Chunk number, 1-based")],
    mode: ModeArg = "auto",
    chunk_size: SizeArg = 0,
    overlap: OverlapArg = 0,
    number_lines: NumberArg = True,
) -> str:
    """Return one chunk of a file with a header (chunk i/N, line range, size) and a footer telling
    you the next call to make. Binary files come back as a hexdump. Leave mode/chunk_size/overlap at
    their defaults to keep using the settings from chunk_file."""
    p = _resolve_file(path)
    idx = _get_index(p, mode, chunk_size, overlap)
    L = idx.loaded
    if idx.count == 0:
        return f"{p.name} is empty (0 bytes)."
    if index > idx.count:
        raise ValueError(
            f"chunk {index} does not exist: {p.name} has {idx.count} chunk(s) "
            f"with mode={idx.mode} chunk_size={idx.size} overlap={idx.overlap}"
        )
    c = idx.chunks[index - 1]
    if L.is_binary:
        body = _hexdump(L.data[c.start : c.end], c.start)
        size = f"{c.length:,} bytes"
    else:
        body = (_numbered(L, c.start, c.end) if number_lines else L.text[c.start : c.end]).rstrip("\n")
        size = f"{c.length:,} chars (~{_tok(c.length):,} tokens)"
    head = (
        f"=== {p.name} | chunk {index}/{idx.count} | {_span(L, c)}"
        f"{'' if L.is_binary else f' of {L.nlines:,}'} | {size} | "
        f"mode={idx.mode} chunk_size={idx.size} overlap={idx.overlap} ==="
    )
    foot = f"=== end of {p.name} ===" if index == idx.count else f'=== next: read_chunk("{p}", {index + 1}) ==='
    return f"{head}\n{body}\n{foot}"


@_tool("read_lines")
def read_lines(
    path: PathArg,
    start_line: Annotated[int, Field(ge=1, description="First line to read (1-based, inclusive)")],
    end_line: Annotated[int, Field(ge=0, description="Last line (inclusive); 0 = start_line + 199")] = 0,
    number_lines: NumberArg = True,
) -> str:
    """Read an exact line range of a text file (max 2000 lines per call). Use after search_file
    to jump straight to the interesting part instead of paging through chunks."""
    p = _resolve_file(path)
    L = _load(p)
    if L.is_binary:
        raise ValueError(f"{p.name} is binary; use read_chunk (mode=bytes) for a hexdump")
    if L.nlines == 0:
        return f"{p.name} is empty."
    if start_line > L.nlines:
        raise ValueError(f"start_line {start_line} is past the end: {p.name} has {L.nlines:,} lines")
    if end_line == 0:
        end_line = start_line + DEFAULT_SIZE["lines"] - 1
    if end_line < start_line:
        raise ValueError("end_line must be >= start_line")
    end_line = min(end_line, L.nlines)
    note = ""
    if end_line - start_line + 1 > MAX_LINES_PER_READ:
        end_line = start_line + MAX_LINES_PER_READ - 1
        note = f" (capped at {MAX_LINES_PER_READ} lines per call)"
    s, e = L.bounds[start_line - 1], L.bounds[end_line]
    body = (_numbered(L, s, e) if number_lines else L.text[s:e]).rstrip("\n")
    head = (
        f"=== {p.name} | lines {start_line}-{end_line} of {L.nlines:,}{note} | "
        f"{e - s:,} chars (~{_tok(e - s):,} tokens) ==="
    )
    foot = "=== end of file ===" if end_line == L.nlines else f'=== next: read_lines("{p}", {end_line + 1}) ==='
    return f"{head}\n{body}\n{foot}"


@_tool("search_file")
def search_file(
    path: PathArg,
    pattern: Annotated[str, Field(min_length=1, description="Regex (default) or plain text to look for")],
    regex: Annotated[bool, Field(description="Treat pattern as a Python regex; false = literal text")] = True,
    ignore_case: Annotated[bool, Field(description="Case-insensitive match")] = False,
    max_hits: Annotated[int, Field(ge=1, le=500, description="Stop after this many matching lines")] = 50,
    context: Annotated[int, Field(ge=0, le=20, description="Lines of context to show around each hit")] = 0,
    after_line: Annotated[int, Field(ge=0, description="Only report hits after this line (to page)")] = 0,
) -> str:
    """Find where something is in a file without reading all of it. Each hit shows the line number,
    the chunk it lives in (with the file's current chunk settings) and the line text, so you can
    follow up with read_lines or read_chunk. Binary files are searched as raw bytes (hits are offsets)."""
    p = _resolve_file(path)
    idx = _get_index(p)
    L = idx.loaded
    flags = re.IGNORECASE if ignore_case else 0
    src = pattern if regex else re.escape(pattern)
    out = [f"{p.name}: /{pattern}/{'i' if ignore_case else ''} ({'regex' if regex else 'literal'})"]

    if L.is_binary:
        try:
            bpat = re.compile(src.encode("latin-1"), flags)
        except (re.error, UnicodeEncodeError) as e:
            raise ValueError(f"bad pattern: {e}") from None
        hits = 0
        for m in bpat.finditer(L.data):
            if hits >= max_hits:
                out.append(f"... stopped after {max_hits} hits")
                break
            hits += 1
            out.append(f"offset {m.start():#x} (chunk {idx.chunk_at(m.start())}): {L.data[m.start():m.start() + 32].hex(' ')}")
        if hits == 0:
            out.append("no matches")
        return "\n".join(out)

    try:
        pat = re.compile(src, flags)
    except re.error as e:
        raise ValueError(f"bad regex: {e}") from None
    hits: list[int] = []
    truncated = False
    last_line = -1
    for m in pat.finditer(L.text, L.bounds[min(after_line, L.nlines)]):
        li = L.line_of(m.start())
        if li == last_line:
            continue
        if len(hits) >= max_hits:
            truncated = True
            break
        hits.append(li)
        last_line = li
    if not hits:
        out.append("no matches" + (f" after line {after_line}" if after_line else ""))
        return "\n".join(out)
    out.append(f"{len(hits)}{'+' if truncated else ''} matching line(s); chunk numbers use mode={idx.mode} chunk_size={idx.size}")
    for li in hits:
        if context:
            for j in range(max(0, li - context), li):
                out.append(f"    L{j + 1}: {L.line_text(j)[:HIT_LINE_CHARS]}")
        txt = L.line_text(li)
        txt = txt[:HIT_LINE_CHARS] + ("..." if len(txt) > HIT_LINE_CHARS else "")
        out.append(f"L{li + 1} (chunk {idx.chunk_at(L.bounds[li])}): {txt}")
        if context:
            for j in range(li + 1, min(L.nlines, li + 1 + context)):
                out.append(f"    L{j + 1}: {L.line_text(j)[:HIT_LINE_CHARS]}")
    if truncated:
        out.append(f'... more hits; search_file(path, pattern, after_line={hits[-1] + 1}) to continue')
    return "\n".join(out)


@_tool("split_to_files", read_only=False)
def split_to_files(
    path: PathArg,
    out_dir: Annotated[str, Field(description="Directory for the part files; '' = <file>_chunks next to the file")] = "",
    mode: ModeArg = "auto",
    chunk_size: SizeArg = 0,
    overlap: OverlapArg = 0,
) -> str:
    """Write the chunks to disk as <name>.partNNN<ext> files plus <name>.manifest.json, for tools
    that need real files. Existing part files with the same names are overwritten."""
    p = _resolve_file(path)
    idx = _get_index(p, mode, chunk_size, overlap)
    L = idx.loaded
    out = _resolve(out_dir) if out_dir else p.with_name(p.name + "_chunks")
    if out.exists() and not out.is_dir():
        raise NotADirectoryError(f"{out} exists and is not a directory")
    out.mkdir(parents=True, exist_ok=True)
    ext = ".bin" if L.is_binary else (p.suffix or ".txt")
    width = max(3, len(str(idx.count)))
    manifest = {
        "source": str(p), "size": L.size, "encoding": L.encoding,
        "mode": idx.mode, "chunk_size": idx.size, "overlap": idx.overlap, "chunks": [],
    }
    for c in idx.chunks:
        name = f"{p.stem}.part{c.index:0{width}d}{ext}"
        target = out / name
        if L.is_binary:
            target.write_bytes(L.data[c.start : c.end])
        else:
            target.write_text(L.text[c.start : c.end], encoding=L.encoding, newline="")
        manifest["chunks"].append({
            "index": c.index, "file": name, "start": c.start, "end": c.end,
            "start_line": c.start_line, "end_line": c.end_line, "length": c.length,
        })
    (out / f"{p.stem}.manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return f"wrote {idx.count} part file(s) + {p.stem}.manifest.json to {out}\n" + _manifest(idx, 1, 50)


# ---------------------------------------------------------------- entry point


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="file_chunker_mcp.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--roots", help=f"directories the server may read, separated by '{os.pathsep}' (same as CHUNKER_ROOTS)")
    args = ap.parse_args()
    if args.roots:
        ROOTS[:] = [Path(r).expanduser().resolve() for r in args.roots.split(os.pathsep) if r.strip()]
    if ROOTS:
        print(f"file_chunker_mcp: restricted to {', '.join(map(str, ROOTS))}", file=sys.stderr)

    if args.transport == "stdio":
        mcp.run("stdio")
    elif _SDK_V2:
        mcp.run(args.transport, host=args.host, port=args.port)
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(args.transport)


if __name__ == "__main__":
    main()
