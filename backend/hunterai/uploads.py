"""Store attached files and turn them into message content blocks."""
from __future__ import annotations

import hashlib
import mimetypes
import os

from . import config
from .db import new_id, now
from .types import Block, FileBlock, ImageBlock, Upload

MAX_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def clean_filename(name: str) -> str:
    """The display name only. It is never used to build a path."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:255] or "upload"


def detect_mime(filename: str, data: bytes) -> str:
    image = sniff_image(data)
    if image:
        return image
    guessed = mimetypes.guess_type(filename)[0]
    if _decode(data) is not None:
        return guessed if guessed and guessed.startswith("text/") else "text/plain"
    if guessed and not guessed.startswith(("text/", "image/")):
        return guessed
    return "application/octet-stream"


def _row(r) -> Upload:
    return Upload(id=r["id"], chat_id=r["chat_id"], filename=r["filename"], mime=r["mime"],
                  size=r["size"], sha256=r["sha256"], created_at=r["created_at"])


async def save(conn, chat_id: str | None, filename: str, data: bytes) -> Upload:
    if len(data) > MAX_BYTES:
        raise ValueError("file too large")
    name = clean_filename(filename)
    digest = hashlib.sha256(data).hexdigest()
    path = config.uploads_dir() / digest
    if not path.exists():
        tmp = path.with_name(f".{digest}.{new_id()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    uid = new_id()
    await conn.execute(
        "insert into uploads(id,chat_id,filename,mime,size,sha256,path,created_at)"
        " values(?,?,?,?,?,?,?,?)",
        (uid, chat_id, name, detect_mime(name, data), len(data), digest, digest, now()))
    await conn.commit()
    return await get(conn, uid)


async def get(conn, upload_id: str) -> Upload | None:
    r = await (await conn.execute("select * from uploads where id=?", (upload_id,))).fetchone()
    return _row(r) if r else None


async def load_bytes(conn, upload_id: str) -> bytes:
    u = await get(conn, upload_id)
    if u is None:
        raise KeyError(upload_id)
    return (config.uploads_dir() / u.sha256).read_bytes()


async def to_block(conn, upload_id: str) -> Block:
    u = await get(conn, upload_id)
    if u is None:
        raise KeyError(upload_id)
    if u.mime in IMAGE_MIMES:
        return ImageBlock(upload_id=u.id, mime=u.mime)
    text = _decode(await load_bytes(conn, upload_id))
    if text is None:
        return FileBlock(upload_id=u.id, filename=u.filename, text=None, size=u.size)
    return FileBlock(upload_id=u.id, filename=u.filename, text=text[:MAX_TEXT_CHARS],
                     truncated=len(text) > MAX_TEXT_CHARS, size=u.size)
