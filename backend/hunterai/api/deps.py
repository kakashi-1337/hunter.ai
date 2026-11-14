from __future__ import annotations

from fastapi import HTTPException

from .. import config, db, uploads
from ..repo import settings

DEFAULT_PROVIDER = "abliteration"
DEFAULT_MODEL = "abliterated-model-large-v2"  # text only; the flash model takes images


async def get_conn():
    conn = await db.connect()
    try:
        yield conn
    finally:
        await conn.close()


def not_found(what: str) -> HTTPException:
    return HTTPException(404, f"{what} not found")


async def default_provider_model(conn) -> tuple[str, str]:
    provider = await settings.get(conn, "default_provider") or DEFAULT_PROVIDER
    model = await settings.get(conn, "default_model")
    if not model:
        model = (config.env("ABLIT_MODEL") or DEFAULT_MODEL) if provider == DEFAULT_PROVIDER \
            else DEFAULT_MODEL
    return provider, model


async def load_upload_bytes(upload_id: str) -> bytes:
    """Standalone loader handed to the run engine (it outlives the request's connection)."""
    conn = await db.connect()
    try:
        return await uploads.load_bytes(conn, upload_id)
    finally:
        await conn.close()
