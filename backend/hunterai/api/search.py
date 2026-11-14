from __future__ import annotations

from fastapi import APIRouter, Depends

from ..repo import search
from .deps import get_conn

router = APIRouter(prefix="/api")


@router.get("/search")
async def search_messages(q: str = "", conn=Depends(get_conn)):
    return await search.query(conn, q)
