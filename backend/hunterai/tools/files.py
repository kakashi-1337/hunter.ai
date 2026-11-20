"""File tools. `read` goes through the owner's file chunker, so a file of any size is read
in full, chunk by chunk; `write` and `edit` change one file atomically and show their diff
in the permission prompt."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .. import mcp_manager
from . import resolve, runner
from .base import PathUse, Prepared, Tool, ToolContext, ToolError, ToolResult

READ_CHUNK_CHARS = 12_000
READ_CHUNK_BYTES = 4_096
READ_MAX_CHARS = 24_000
SEARCH_HITS = 50
CHUNKER_TIMEOUT = 30.0
FALLBACK_LINES = 200
FALLBACK_LINE_CHARS = 2_000
MAX_WRITE_BYTES = 2 * 1024**2
MAX_EDIT_BYTES = 2 * 1024**2
PREVIEW_CHARS = 4_000
LARGE_LINES = 30            # a change that takes this many lines out of a file asks the owner first, whatever the settings say
LARGE_SHARE_FROM = 10       # ... and so does one that takes out half or more of a file of this many lines or more
LARGE_REASON = "a change that takes away this much of a file always asks first, whatever the settings say"

_INFO_KEYS = ("path:", "size:", "type:", "lines:", "chars:", "note:")


# ---------------------------------------------------------------- read

async def _prepare_read(ctx: ToolContext, args: dict) -> Prepared:
    if args.get("search") is not None and (args.get("chunk") or args.get("start_line")):
        raise ToolError("search cannot be combined with chunk or start_line")
    p = resolve.resolve_path(args["path"], ctx.workdir)
    if not p.is_file():
        raise ToolError(f"not a file: {p}" if p.exists() else f"no such file: {p}")
    return Prepared(paths=(PathUse(str(p), "read"),), grant_target=str(p))


def _own_footers(text: str, p: str, search: str | None = None) -> str:
    """Rewrite the chunker's "next call" hints to this tool's own call form."""
    q = json.dumps(p)
    text = re.sub(r'=== next: read_chunk\("[^"\n]*", (\d+)\) ===',
                  lambda m: f"=== next: read(path={q}, chunk={m.group(1)}) ===", text)
    text = re.sub(r'=== next: read_lines\("[^"\n]*", (\d+)\) ===',
                  lambda m: f"=== next: read(path={q}, start_line={m.group(1)}) ===", text)
    if search is not None:
        text = re.sub(r"search_file\(path, pattern, after_line=(\d+)\)",
                      lambda m: f"read(path={q}, search={json.dumps(search)}, "
                                f"after_line={m.group(1)})", text)
    return text


def _fit_lines(text: str, p: str) -> str:
    """Keep the header and as many whole numbered lines as fit; never cut inside the text."""
    lines = text.split("\n")
    if len(lines) < 3 or not lines[0].startswith("==="):
        return text
    head, body, foot = lines[0], lines[1:-1], lines[-1]
    budget = READ_MAX_CHARS - len(head) - len(foot) - 2 - 80   # room for a rewritten footer
    kept, used = [], 0
    for ln in body:
        if used + len(ln) + 1 > budget:
            break
        kept.append(ln)
        used += len(ln) + 1
    if len(kept) == len(body):
        return text
    q = json.dumps(p)
    first_dropped = int(body[len(kept)].split("\t", 1)[0])
    if not kept:
        k = len(body[0].split("\t", 1)[-1])
        raise ToolError(
            f"line {first_dropped} of {p} is {k:,} characters, more than a line read returns "
            f"({READ_MAX_CHARS:,}). Chunks split long lines: read(path={q}, chunk=1). "
            f"To skip this line: read(path={q}, start_line={first_dropped + 1}).")
    head = re.sub(r"lines (\d+)-\d+", lambda m: f"lines {m.group(1)}-{first_dropped - 1}",
                  head, count=1)
    foot = f"=== next: read(path={q}, start_line={first_dropped}) ==="
    return "\n".join([head, *kept, foot])


class _Down(Exception):
    """The chunker itself cannot be used (as opposed to an error it reports for the file)."""


async def _via_chunker(manager, p: str, args: dict) -> ToolResult:
    async def call(tool: str, **kw) -> ToolResult:
        try:
            return await manager.call_tool("chunker", tool, {"path": p, **kw},
                                           timeout=CHUNKER_TIMEOUT)
        except ToolError as e:
            raise _Down(str(e)) from None

    info = await call("file_info")
    if info.is_error:
        return info
    header = [ln for ln in info.text.splitlines() if ln.startswith(_INFO_KEYS)]
    binary = any(ln.startswith("type: binary") for ln in header)

    if args.get("search") is not None:
        context = int(args.get("context") or 0)
        r = await call("search_file", pattern=args["search"], regex=bool(args.get("regex", False)),
                       ignore_case=bool(args.get("ignore_case", False)),
                       max_hits=max(1, SEARCH_HITS // (1 + 2 * context)), context=context,
                       after_line=int(args.get("after_line") or 0))
        if r.is_error:
            return r
        # Chunk numbers in the hits come from the chunker's own default chunking, not ours.
        text = re.sub(r" \(chunk \d+\)", "", r.text)
        text = re.sub(r"; chunk numbers use [^\n]*", "", text)
        text = _own_footers(text, p, args["search"])
        return ToolResult(f"{text}\nread a hit with read(path={json.dumps(p)}, start_line=<line>)")

    if args.get("start_line"):
        r = await call("read_lines", start_line=int(args["start_line"]),
                       end_line=int(args.get("end_line") or 0), number_lines=True)
        if r.is_error:
            return r
        return ToolResult(_fit_lines(_own_footers(r.text, p), p))

    index = int(args.get("chunk") or 1)
    size = {"mode": "bytes", "chunk_size": READ_CHUNK_BYTES} if binary \
        else {"mode": "chars", "chunk_size": READ_CHUNK_CHARS}
    r = await call("read_chunk", index=index, overlap=0, number_lines=True, **size)
    if not r.is_error and len(r.text) > runner.RESULT_CAP:
        # Line numbers pushed it over the result cap: a chunk is never cut, so drop them.
        r = await call("read_chunk", index=index, overlap=0, number_lines=False, **size)
    if r.is_error:
        return r
    text = _own_footers(r.text, p)
    if not args.get("chunk"):
        text = "\n".join(header) + "\n\n" + text
    return ToolResult(text)


def _fallback(p: str, args: dict, reason: str) -> ToolResult:
    """A bounded plain read for when the chunker is not usable. Line by line, never whole."""
    if args.get("search") is not None:
        raise ToolError("search needs the chunker; use repo_search")
    with open(p, "rb") as f:
        if b"\x00" in f.read(8192):
            raise ToolError("binary file; reading it needs the chunker")
    start = int(args.get("start_line") or ((int(args.get("chunk") or 1) - 1) * FALLBACK_LINES + 1))
    last = start + FALLBACK_LINES - 1
    if args.get("end_line"):
        last = min(last, int(args["end_line"]))
    q = json.dumps(p)
    out = [f"[chunker unavailable ({reason}): plain read, {FALLBACK_LINES} lines per call]"]
    used, n, more, next_line = len(out[0]) + 80, 0, False, None
    with open(p, encoding="utf-8", errors="replace", newline="") as f:
        for n, raw in enumerate(f, 1):
            if n < start:
                continue
            if n > last:
                more, next_line = True, n
                break
            text = raw.rstrip("\r\n")
            if len(text) > FALLBACK_LINE_CHARS:
                text = (text[:FALLBACK_LINE_CHARS]
                        + f" [line cut at {FALLBACK_LINE_CHARS:,} of {len(text):,} characters]")
            line = f"{n}\t{text}"
            if used + len(line) + 1 > READ_MAX_CHARS and len(out) > 1:
                more, next_line = True, n
                break
            out.append(line)
            used += len(line) + 1
    if len(out) == 1 and not more:
        raise ToolError(f"start_line {start} is past the end: {p} has {n:,} lines")
    if more and args.get("end_line") and next_line > int(args["end_line"]):
        out.append("=== end of the requested range ===")
    elif more:
        out.append(f"=== next: read(path={q}, start_line={next_line}) ===")
    else:
        out.append("=== end of file ===")
    return ToolResult("\n".join(out), meta={"fallback": True})


async def _read(ctx: ToolContext, args: dict) -> ToolResult:
    p = ctx.prepared.paths[0].path           # the resolved path that was checked
    manager = mcp_manager.get()
    if manager is None:
        reason = "the MCP manager is not running"
    elif not manager.available("chunker"):
        reason = "the chunker server is not running"
    else:
        try:
            return await _via_chunker(manager, p, args)
        except _Down as e:
            reason = str(e)
    return _fallback(p, args, reason)


# ---------------------------------------------------------------- write, edit

def _sha(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _cut(preview: str) -> str:
    return preview if len(preview) <= PREVIEW_CHARS else preview[:PREVIEW_CHARS] + "\n[preview cut]"


def _diff(old: str, new: str, p: Path) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True),
                                        new.splitlines(keepends=True),
                                        fromfile=f"{p} (current)", tofile=f"{p} (new)", n=3))


def _removed(diff: str) -> int:
    """Lines of the current content that a change takes away, counted in the comparison that is shown."""
    return sum(1 for line in diff.split("\n@@", 1)[-1].splitlines() if line.startswith("-")) if "\n@@" in diff else 0


def large_removal(state: dict) -> str | None:
    """The warning for a change that takes away a large part of a file, from what its prepare step counted;
    None for any other change. Such a change asks the owner first, whatever the settings say (see the runner)."""
    if state.get("not_text"):
        return f"this replaces the whole of {state.get('name')}, which is not text ({state['not_text']:,} bytes)"
    removes, of = state.get("removes") or 0, state.get("of") or 0
    if removes >= LARGE_LINES or (of >= LARGE_SHARE_FROM and removes * 2 >= of):
        return f"this change removes {removes:,} of the {of:,} lines of {state.get('name')}"
    return None


def _text_of(p: Path) -> str | None:
    try:
        return p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _write_atomic(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = (p.stat().st_mode & 0o7777) if p.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _same_as_shown(ctx: ToolContext, args: dict) -> Path:
    """The file must still be the one whose change the owner was shown."""
    shown = Path(ctx.prepared.paths[0].path)
    if resolve.resolve_path(args["path"], ctx.workdir) != shown \
            or _sha(shown) != ctx.prepared.state.get("sha256"):
        raise ToolError(f"{shown} changed since it was shown; call {ctx.prepared.state['tool']} again")
    return shown


async def _prepare_write(ctx: ToolContext, args: dict) -> Prepared:
    p = resolve.resolve_path(args["path"], ctx.workdir)
    if p.is_dir():
        raise ToolError(f"{p} is a directory")
    data = args["content"].encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise ToolError(f"content is too large: {len(data):,} bytes (limit {MAX_WRITE_BYTES:,})")
    old = _text_of(p) if p.exists() else None
    state = {"sha256": _sha(p), "tool": "write", "name": p.name}
    if p.exists():
        head = f"overwrite {p} ({p.stat().st_size:,} → {len(data):,} bytes)"
        if old is not None:
            body = _diff(old, args["content"], p)
            state.update(removes=_removed(body), of=len(old.splitlines()))
        else:
            body = "\n".join(args["content"].splitlines()[:60])
            state.update(not_text=p.stat().st_size)
    else:
        head = f"create {p} ({len(data):,} bytes)"
        body = "\n".join(args["content"].splitlines()[:60])
    return Prepared(paths=(PathUse(str(p), "write"),), grant_target=str(p),
                    preview=_cut(f"{head}\n{body}"), state=state)


async def _write(ctx: ToolContext, args: dict) -> ToolResult:
    p = _same_as_shown(ctx, args)
    data = args["content"].encode("utf-8")
    _write_atomic(p, data)
    return ToolResult(f"wrote {len(data)} bytes to {p}")


def _edited(p: Path, args: dict) -> tuple[str, int]:
    if not p.is_file():
        raise ToolError(f"no such file: {p}")
    if p.stat().st_size > MAX_EDIT_BYTES:
        raise ToolError(f"{p} is too large to edit ({p.stat().st_size:,} bytes, "
                        f"limit {MAX_EDIT_BYTES:,})")
    text = _text_of(p)
    if text is None:
        raise ToolError(f"{p} is not UTF-8 text")
    old, new = args["old_string"], args["new_string"]
    if old == new:
        raise ToolError("old_string and new_string are the same")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old_string not found in {p}")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_string matches {count} times in {p}; add surrounding lines to "
                        "make it unique or set replace_all")
    return text.replace(old, new), count


async def _prepare_edit(ctx: ToolContext, args: dict) -> Prepared:
    p = resolve.resolve_path(args["path"], ctx.workdir)
    new_text, count = _edited(p, args)
    old_text = _text_of(p)
    diff = _diff(old_text, new_text, p)
    return Prepared(paths=(PathUse(str(p), "write"),), grant_target=str(p),
                    preview=_cut(f"edit {p} ({count} replacement(s))\n" + diff),
                    state={"sha256": _sha(p), "tool": "edit", "name": p.name, "removes": _removed(diff), "of": len(old_text.splitlines())})


async def _edit(ctx: ToolContext, args: dict) -> ToolResult:
    p = _same_as_shown(ctx, args)
    new_text, count = _edited(p, args)
    _write_atomic(p, new_text.encode("utf-8"))
    return ToolResult(f"replaced {count} occurrence(s) in {p}")


def tools() -> list[Tool]:
    return [
        Tool("read", (
            "Read a file. Large files come in chunks: the result ends with the exact call that "
            "returns the next part. Use `search` to find lines, `start_line`/`end_line` for an "
            "exact range, `chunk` to page through the whole file."),
            {"type": "object", "properties": {
                "path": {"type": "string"}, "chunk": {"type": "integer", "minimum": 1},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "search": {"type": "string", "minLength": 1}, "regex": {"type": "boolean"},
                "ignore_case": {"type": "boolean"},
                "context": {"type": "integer", "minimum": 0, "maximum": 20},
                "after_line": {"type": "integer", "minimum": 0}},
             "required": ["path"], "additionalProperties": False},
            "read", _read, prepare=_prepare_read),
        Tool("write", "Create a file or replace its whole content. Parent folders are created.",
             {"type": "object", "properties": {"path": {"type": "string"},
                                               "content": {"type": "string"}},
              "required": ["path", "content"], "additionalProperties": False},
             "write", _write, prepare=_prepare_write),
        Tool("edit", (
            "Replace an exact string in a text file. old_string must match exactly once unless "
            "replace_all is true; include surrounding lines to make it unique."),
            {"type": "object", "properties": {
                "path": {"type": "string"}, "old_string": {"type": "string", "minLength": 1},
                "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
             "required": ["path", "old_string", "new_string"], "additionalProperties": False},
            "write", _edit, prepare=_prepare_edit),
    ]
