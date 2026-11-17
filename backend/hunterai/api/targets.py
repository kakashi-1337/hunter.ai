from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, field_validator

from .. import brief
from ..repo import targets
from ..tools import resolve
from .deps import get_conn, not_found

router = APIRouter(prefix="/api/targets")


class Scope(BaseModel):
    model_config = {"populate_by_name": True}
    in_: list[str] = []
    out: list[str] = []

    def __init__(self, **data):
        if "in" in data:
            data["in_"] = data.pop("in")
        super().__init__(**data)

    def as_dict(self) -> dict:
        return {"in": self.in_, "out": self.out}


_SIDES = (("in", "In scope"), ("out", "Out of scope"))


def _problems(scope: dict) -> list[dict]:
    """The lines of a stored scope that cannot be read. Each is checked against nothing until the owner rewrites it."""
    return [{"side": side, "line": line, "why": why}
            for side, _ in _SIDES for line, why in resolve.unreadable(scope.get(side) or [])]


def _view(target) -> dict:
    return {**target.model_dump(), "scope_problems": _problems(target.scope)}


def _checked(scope: Scope) -> dict:
    """The two lists as they are stored: one entry a line, trimmed. A line that cannot be read is refused here,
    because it would be kept and then checked against nothing."""
    lists = {"in": [x.strip() for x in scope.in_ if x.strip()], "out": [x.strip() for x in scope.out if x.strip()]}
    bad = [f"{label}: {p['line']} ({p['why']})" for side, label in _SIDES for p in _problems(lists) if p["side"] == side]
    if bad:
        raise HTTPException(422, "These scope lines cannot be read, so nothing would be checked against them. "
                                 "Rewrite or remove them:\n" + "\n".join(bad))
    return lists


class TargetIn(BaseModel):
    name: str
    notes: str = ""
    scope: Scope | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be empty")
        return v.strip()


class TargetPatch(BaseModel):
    name: str | None = None
    notes: str | None = None
    scope: Scope | None = None


@router.get("")
async def list_targets(conn=Depends(get_conn)):
    return [_view(t) for t in await targets.list(conn)]


@router.post("")
async def create_target(body: TargetIn, conn=Depends(get_conn)):
    return _view(await targets.create(conn, body.name, body.notes, _checked(body.scope) if body.scope else None))


class BriefIn(BaseModel):
    text: str


@router.post("/brief")
async def read_brief(body: BriefIn):
    """What a pasted program brief says about scope. Reads only: the owner reviews it before saving."""
    try:
        return (await asyncio.to_thread(brief.read, body.text)).as_dict()
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.patch("/{tid}")
async def update_target(tid: str, body: TargetPatch, conn=Depends(get_conn)):
    if not await targets.get(conn, tid):
        raise not_found("target")
    fields = body.model_dump(exclude_unset=True, exclude={"scope"})
    if body.scope is not None:
        fields["scope"] = _checked(body.scope)
    if "name" in fields and not (fields["name"] or "").strip():
        fields.pop("name")
    return _view(await targets.update(conn, tid, **fields))


@router.delete("/{tid}", status_code=204)
async def delete_target(tid: str, conn=Depends(get_conn)):
    await targets.delete(conn, tid)
    return Response(status_code=204)


@router.get("/{tid}/findings")
async def list_findings(tid: str, conn=Depends(get_conn)):
    from ..repo import findings
    return await findings.list_for_target(conn, tid)

