from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, field_validator

from ..repo import targets
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
    return await targets.list(conn)


@router.post("")
async def create_target(body: TargetIn, conn=Depends(get_conn)):
    return await targets.create(conn, body.name, body.notes,
                                body.scope.as_dict() if body.scope else None)


@router.patch("/{tid}")
async def update_target(tid: str, body: TargetPatch, conn=Depends(get_conn)):
    if not await targets.get(conn, tid):
        raise not_found("target")
    fields = body.model_dump(exclude_unset=True, exclude={"scope"})
    if body.scope is not None:
        fields["scope"] = body.scope.as_dict()
    if "name" in fields and not (fields["name"] or "").strip():
        fields.pop("name")
    return await targets.update(conn, tid, **fields)


@router.delete("/{tid}", status_code=204)
async def delete_target(tid: str, conn=Depends(get_conn)):
    await targets.delete(conn, tid)
    return Response(status_code=204)


@router.get("/{tid}/findings")
async def list_findings(tid: str, conn=Depends(get_conn)):
    from ..repo import findings
    return await findings.list_for_target(conn, tid)

