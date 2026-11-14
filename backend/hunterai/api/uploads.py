from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Response, UploadFile

from .. import uploads
from .deps import get_conn, not_found

router = APIRouter(prefix="/api/uploads")


@router.post("")
async def upload(file: UploadFile, chat_id: str | None = Form(None), conn=Depends(get_conn)):
    data = await file.read(uploads.MAX_BYTES + 1)
    try:
        return await uploads.save(conn, chat_id, file.filename or "upload", data)
    except ValueError as e:
        raise HTTPException(413, str(e))


@router.get("/{uid}")
async def download(uid: str, conn=Depends(get_conn)):
    u = await uploads.get(conn, uid)
    if not u:
        raise not_found("upload")
    data = await uploads.load_bytes(conn, uid)
    is_image = u.mime in uploads.IMAGE_MIMES
    # Only real images render in the browser; everything else downloads as opaque bytes,
    # so an uploaded HTML or SVG file can never run script in the app's origin.
    return Response(content=data, media_type=u.mime if is_image else "application/octet-stream",
                    headers={
                        "Content-Disposition":
                            f"{'inline' if is_image else 'attachment'}; "
                            f"filename*=UTF-8''{quote(u.filename)}",
                        "X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": "default-src 'none'; sandbox",
                    })
