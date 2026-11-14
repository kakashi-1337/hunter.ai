"""Paths and environment lookup. Nothing here caches, so tests can repoint it freely."""
from __future__ import annotations

import os
from pathlib import Path

# The owner's checkout: where .env lives and which folders tools must never write. A deployed
# copy of the app (scripts/deploy.sh) points back at it with HUNTERAI_PROJECT.
PROJECT_ROOT = Path(os.environ.get("HUNTERAI_PROJECT") or Path(__file__).resolve().parents[2])


def home() -> Path:
    p = Path(os.environ.get("HUNTERAI_HOME") or Path.home() / ".hunterai")
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return home() / "hunterai.db"


def uploads_dir() -> Path:
    p = home() / "uploads"
    p.mkdir(parents=True, exist_ok=True)
    return p


def env_file() -> Path:
    return Path(os.environ.get("HUNTERAI_ENV_FILE") or PROJECT_ROOT / ".env")


def dist_dir() -> Path:
    """Where the built frontend lives (frontend/dist unless HUNTERAI_DIST says otherwise)."""
    return Path(os.environ.get("HUNTERAI_DIST") or PROJECT_ROOT / "frontend" / "dist")


DEFAULT_PORT = 8790
DEV_PORT = 5173  # Vite


def port() -> int:
    try:
        return int(os.environ.get("HUNTERAI_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def dev() -> bool:
    return os.environ.get("HUNTERAI_DEV") == "1"


def allowed_origins() -> set[str]:
    """The app's own origins. The Vite dev origin counts only in development."""
    ports = [port()] + ([DEV_PORT] if dev() else [])
    return {f"http://{h}:{p}" for p in ports for h in ("127.0.0.1", "localhost")}


def _dir(*parts: str) -> Path:
    p = home().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def work_dir(chat_id: str) -> Path:
    return _dir("work", chat_id)


def proc_dir() -> Path:
    return _dir("proc")


def output_dir() -> Path:
    return _dir("tool-output")


def logs_dir() -> Path:
    return _dir("logs")


def mcp_file() -> Path:
    return home() / "mcp.json"


def chunker_path() -> Path:
    """The file chunker MCP server: the one that ships with HunterAI, or HUNTERAI_CHUNKER."""
    return Path(os.environ.get("HUNTERAI_CHUNKER") or Path(__file__).resolve().parent / "mcp_servers" / "file_chunker_mcp.py")


def mcp_enabled() -> bool:
    return os.environ.get("HUNTERAI_MCP") != "off"


def searxng_url() -> str:
    return os.environ.get("HUNTERAI_SEARXNG_URL") or "http://127.0.0.1:8888"


def parse_env_line(line: str) -> tuple[str, str] | None:
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    name, _, value = s.partition("=")
    name = name.removeprefix("export ").strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return name, value


def env(name: str) -> str | None:
    """Look up one variable: process environment first, then the env file.

    The file is read on every call and only the requested name is returned;
    it is never exported into os.environ, so unrelated variables stay untouched.
    """
    v = os.environ.get(name)
    if v:
        return v
    try:
        raw = env_file().read_bytes()
    except OSError:
        return None
    # Split on real newlines only (as envfile.set_var does), then decode each line;
    # a stray non-UTF-8 byte must not hide the valid lines.
    for chunk in raw.splitlines():
        kv = parse_env_line(chunk.decode("utf-8", errors="replace"))
        if kv and kv[0] == name and kv[1]:
            return kv[1]
    return None


def vector_backend() -> str:
    """Where vectors live: "qdrant" (the container) or "memory" (the app database; tests, small use)."""
    v = os.environ.get("HUNTERAI_VECTOR") or "qdrant"
    if v not in ("qdrant", "memory"):
        raise ValueError(f"HUNTERAI_VECTOR must be qdrant or memory, not {v!r}")
    return v


def qdrant_url() -> str:
    return os.environ.get("HUNTERAI_QDRANT_URL") or "http://127.0.0.1:6333"


def staging_dir() -> Path:
    return _dir("staging")


def ocr_enabled() -> bool:
    """Read text from images and scanned PDF pages with tesseract (HUNTERAI_OCR=off turns it off)."""
    return (os.environ.get("HUNTERAI_OCR") or "on").lower() != "off"


def legacy_office() -> bool:
    """Convert old doc/xls/ppt/rtf files with LibreOffice (HUNTERAI_LEGACY_OFFICE=on). Off by
    default: it runs LibreOffice, with its network access, on files that may come from anywhere."""
    return (os.environ.get("HUNTERAI_LEGACY_OFFICE") or "off").lower() == "on"


def tmp_dir() -> Path:
    """Scratch space on the data disk (the system /tmp may be memory)."""
    return _dir("tmp")
