from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter

from ..providers import registry

router = APIRouter(prefix="/api")

MODEL_TIMEOUT = 5.0
CACHE_SECONDS = 300
FAILED_SECONDS = 5  # a failed listing is retried almost at once, but not on every request
_cache: dict[str, tuple[float, dict]] = {}  # provider name -> (expires at, entry)


def clear_cache() -> None:
    _cache.clear()


async def _describe(provider) -> tuple[dict, bool]:
    """A provider's entry, and whether its model listing worked."""
    configured = False
    models: list = []
    try:
        configured = bool(provider.configured())
        if configured:
            models = await asyncio.wait_for(provider.models(), MODEL_TIMEOUT)
    except Exception:
        models = []  # one slow or broken provider must not hide the others
    # Adapters report an unreachable service as an empty list, so that counts as a failure too.
    ok = bool(models) or not configured
    return {"name": provider.name, "configured": configured,
            "models": [m.model_dump() for m in models]}, ok


async def _cached(provider) -> dict:
    hit = _cache.get(provider.name)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    entry, ok = await _describe(provider)
    _cache[provider.name] = (time.monotonic() + (CACHE_SECONDS if ok else FAILED_SECONDS), entry)
    return entry


@router.get("/providers")
async def list_providers():
    return list(await asyncio.gather(*(_cached(p) for p in registry.all())))
