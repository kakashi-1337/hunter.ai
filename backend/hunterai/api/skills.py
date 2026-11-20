from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .. import skills
from ..repo import skills as store
from .deps import get_conn, not_found

router = APIRouter(prefix="/api/skills")


class SkillIn(BaseModel):
    name: str | None = Field(None, max_length=64)
    description: str | None = Field(None, max_length=skills.MAX_DESCRIPTION)
    body: str | None = Field(None, max_length=skills.MAX_BODY)
    mode: Literal["plan", "pentesting", "coding"] | None = None
    enabled: bool | None = None
    auto: bool | None = None


def _clean(body: SkillIn, *, creating: bool) -> dict:
    fields = body.model_dump(exclude_unset=True)
    try:
        if "name" in fields or creating:
            fields["name"] = skills.check_name((fields.get("name") or "").strip())
    except skills.SkillError as e:
        raise HTTPException(400, str(e)) from None
    if "body" in fields or creating:
        fields["body"] = (fields.get("body") or "").strip()
        if not fields["body"]:
            raise HTTPException(400, "a skill needs instructions")
    if "description" in fields:
        fields["description"] = " ".join((fields["description"] or "").split())
    for flag in ("enabled", "auto"):
        if flag in fields and fields[flag] is None:
            fields.pop(flag)
    return fields


@router.get("")
async def list_skills(conn=Depends(get_conn)):
    return await store.list(conn)


@router.post("")
async def write_skill(body: SkillIn, conn=Depends(get_conn)):
    try:
        return await store.create(conn, source="written here", **_clean(body, creating=True))
    except ValueError:
        raise HTTPException(409, "a skill with this name already exists") from None


class SourceIn(BaseModel):
    source: str = Field(max_length=skills.MAX_TEXT)


@router.post("/inspect")
async def inspect_source(body: SourceIn, conn=Depends(get_conn)):
    """Bring a source here and list its skills in full. Nothing is kept as a skill and nothing is run."""
    try:
        return await skills.inspect(conn, body.source)
    except skills.SkillError as e:
        raise HTTPException(400, str(e)) from None


class Pick(BaseModel):
    key: str
    name: str | None = Field(None, max_length=64)


class AddIn(BaseModel):
    id: str
    picks: list[Pick] = Field(min_length=1, max_length=skills.MAX_FOUND)


@router.post("/add")
async def add_skills(body: AddIn, conn=Depends(get_conn)):
    try:
        return await skills.add(conn, body.id, [p.model_dump() for p in body.picks])
    except skills.SkillError as e:
        raise HTTPException(400, str(e)) from None


@router.patch("/{sid}")
async def change_skill(sid: str, body: SkillIn, conn=Depends(get_conn)):
    if not await store.get(conn, sid):
        raise not_found("skill")
    try:
        return await store.update(conn, sid, **_clean(body, creating=False))
    except ValueError:
        raise HTTPException(409, "a skill with this name already exists") from None


@router.delete("/{sid}", status_code=204)
async def delete_skill(sid: str, conn=Depends(get_conn)):
    await skills.remove(conn, sid)
    return Response(status_code=204)
