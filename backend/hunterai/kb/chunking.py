"""Structure-aware chunking. A chunk is a span of the cleaned text, so its offsets and line
numbers point back into the file; sizes and boundaries depend on the kind of file."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..types import ChunkDraft
from .parsers import Kind

MAX_CHUNK_CHARS = 2000
TAIL_MERGE_CHARS = 200
CSV_HEADER_MAX = 400


@dataclass(frozen=True)
class Profile:
    target: int
    overlap: int


PROFILES: dict[Kind, Profile] = {
    "text": Profile(1200, 150), "html": Profile(1200, 150), "pdf": Profile(1200, 150),
    "markdown": Profile(1200, 0), "code": Profile(1500, 0), "sql": Profile(1500, 0), "csv": Profile(1200, 0),
}

Span = tuple[int, int]
Splitter = Callable[[str, int, int], list[int]]    # boundaries strictly inside (start, end)


def _by_regex(pattern: str) -> Splitter:
    rx = re.compile(pattern)
    return lambda text, s, e: [m.end() for m in rx.finditer(text, s, e) if s < m.end() < e]


def _lines(text: str, s: int, e: int):
    """(start of line, line text) for every line that starts inside [s, e)."""
    pos = s
    while pos < e:
        nl = text.find("\n", pos, e)
        end = e if nl < 0 else nl + 1
        yield pos, text[pos:end]
        pos = end


def _headings(text: str, s: int, e: int) -> list[int]:
    out, fenced = [], False
    for pos, line in _lines(text, s, e):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and pos > s and re.match(r"#{1,6}\s", line):
            out.append(pos)
    return out


def _top_level(text: str, s: int, e: int) -> list[int]:
    out, blank_before = [], False
    for pos, line in _lines(text, s, e):
        if pos > s and blank_before and line.strip() and not line[0].isspace():
            out.append(pos)
        blank_before = not line.strip()
    return out


def _statements(text: str, s: int, e: int) -> list[int]:
    return [pos + len(line) for pos, line in _lines(text, s, e)
            if line.rstrip().endswith(";") and pos + len(line) < e]


_BLANK = _by_regex(r"\n[ \t]*\n\s*")
_LINE = _by_regex(r"\n")
_SENTENCE = _by_regex(r"[.?!] ")
_SPACE = _by_regex(r"\s+")

LEVELS: dict[Kind, list[Splitter]] = {
    "text": [_BLANK, _LINE, _SENTENCE, _SPACE], "html": [_BLANK, _LINE, _SENTENCE, _SPACE],
    "pdf": [_BLANK, _LINE, _SENTENCE, _SPACE], "markdown": [_headings, _BLANK, _LINE, _SPACE],
    "code": [_top_level, _LINE, _SPACE], "sql": [_statements, _LINE, _SPACE], "csv": [_LINE, _SPACE],
}
_LEADING = re.compile(r"(?:[ \t\r\f\v]*\n)+")


def _trim(text: str, s: int, e: int) -> Span:
    """No leading blank lines, no trailing whitespace; the first line keeps its indentation."""
    e = s + len(text[s:e].rstrip())
    m = _LEADING.match(text, s, e)
    if m:
        s = m.end()
    return s, e


def _size(text: str, s: int, e: int) -> int:
    s, e = _trim(text, s, e)
    return max(0, e - s)


def _split(text: str, s: int, e: int, levels: list[Splitter], level: int, target: int, overlap: int) -> list[Span]:
    """Spans that partition [s, e), each within `target` after trimming (plus overlap)."""
    if level >= len(levels):
        return [(i, min(i + target, e)) for i in range(s, e, target)]
    edges = [s, *levels[level](text, s, e), e]     # no boundary here: one unit, split finer below
    out: list[Span] = []
    cur: Span | None = None
    for a, b in zip(edges, edges[1:]):
        if _size(text, a, b) > target:
            if cur:
                out.append(cur); cur = None
            parts = _split(text, a, b, levels, level + 1, target, overlap)
            out.extend(_overlapped(text, parts, overlap) if level == 0 and overlap else parts)
        elif cur and _size(text, cur[0], b) <= target:
            cur = (cur[0], b)
        else:
            if cur:
                out.append(cur)
            cur = (a, b)
    if cur:
        out.append(cur)
    return out


def _overlapped(text: str, parts: list[Span], overlap: int) -> list[Span]:
    """Pieces of one paragraph: each later piece starts a little before the previous one ended,
    at a line start if there is one in the window, else just after whitespace."""
    out = [parts[0]] if parts else []
    for (ps, _), (s, e) in zip(parts, parts[1:]):
        lo = max(s - overlap, ps + 1)
        line = next((p for p in range(lo, s) if text[p - 1] == "\n"), None)
        space = next((p for p in range(lo, s) if text[p - 1].isspace() and not text[p].isspace()), None)
        start = line if line is not None else space if space is not None else s
        out.append((start, e))
    return out


def chunk(text: str, kind: Kind) -> list[ChunkDraft]:
    profile = PROFILES[kind]
    header, body_start, target = "", 0, profile.target
    if kind == "csv":
        nl = text.find("\n")
        first = text if nl < 0 else text[:nl]
        if nl >= 0 and len(first) <= CSV_HEADER_MAX:
            header = first
            target = max(200, profile.target - len(header) - 1)
    spans = [_trim(text, s, e) for s, e in
             _split(text, body_start, len(text), LEVELS[kind], 0, target, profile.overlap)]
    spans = [(s, e) for s, e in spans if e > s]
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] < TAIL_MERGE_CHARS:
        (ps, _), (_, le) = spans[-2], spans[-1]
        if le - ps + (len(header) + 1 if header else 0) <= MAX_CHUNK_CHARS:
            spans[-2:] = [(ps, le)]
    out = []
    for i, (s, e) in enumerate(spans):
        body = text[s:e]
        # Every csv chunk after the first carries the header row; its offsets describe the rows.
        shown = f"{header}\n{body}" if header and s > 0 else body
        out.append(ChunkDraft(ordinal=i, text=shown, char_start=s, char_end=e,
                              line_start=text.count("\n", 0, s) + 1, line_end=text.count("\n", 0, e - 1) + 1))
    return out
