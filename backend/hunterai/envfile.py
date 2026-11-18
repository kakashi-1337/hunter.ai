"""Change one variable in the .env file and nothing else.

The file is shared with other tools (it holds unrelated variables), so every
other byte is preserved and only an allow-listed name can be written.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ALLOWED = {"ABLIT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
           "GEMINI_API_KEY", "ABLIT_MODEL", "DISCORD_WEBHOOK_URL", "HUNTERAI_PROVIDER_PROXY",
           "HUNTERAI_OLLAMA_NUM_CTX", "HUNTERAI_COLIBRI_URL", "COLIBRI_API_KEY"}


def _is_assignment(line: bytes, name: bytes) -> bool:
    s = line.lstrip()
    if s.startswith(b"export "):
        s = s[len(b"export "):].lstrip()
    return s.startswith(name) and s[len(name):].lstrip().startswith(b"=")


def _ending(line: bytes) -> bytes:
    for e in (b"\r\n", b"\n", b"\r"):
        if line.endswith(e):
            return e
    return b""


def set_var(path: Path, name: str, value: str) -> None:
    if name not in ALLOWED:
        raise ValueError(f"{name!r} cannot be set from the app")
    value = value.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("value contains control characters")
    path = Path(path)
    # Bytes throughout: no newline translation, and a stray non-UTF-8 byte is just a byte.
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        data = b""
    key = name.encode()
    assignment = key + b"=" + value.encode("utf-8")
    lines = data.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _is_assignment(line, key):
            prefix = line[: line.index(key)]
            lines[i] = prefix + assignment + _ending(line)
            break
    else:
        eol = next((e for e in map(_ending, lines) if e), b"\n")  # the file's own ending
        if lines and not _ending(lines[-1]):
            lines[-1] += eol
        lines.append(assignment + eol)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:  # mkstemp creates the file with mode 600
            f.write(b"".join(lines))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
