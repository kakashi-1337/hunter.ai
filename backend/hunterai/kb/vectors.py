"""Where chunk vectors live. Two stores behind one interface: Qdrant (the container) and an
exact-cosine store in the app database, shared by every process on the same data folder."""
from __future__ import annotations

import math
import uuid
from array import array
from dataclasses import dataclass
from typing import Protocol

from .. import config, db


@dataclass(frozen=True)
class Point:
    id: str             # a chunk id, 32 hex characters
    document_id: str
    vector: list[float]


class VectorError(Exception):
    pass


class VectorStore(Protocol):
    async def ensure_collection(self, name: str, dim: int) -> None: ...
    async def upsert(self, name: str, points: list[Point]) -> None: ...
    async def delete(self, name: str, ids: list[str]) -> None: ...
    async def delete_by_document(self, name: str, document_id: str) -> None: ...
    async def search(self, name: str, vector: list[float], limit: int) -> list[tuple[str, float]]: ...
    async def count(self, name: str) -> int: ...
    async def drop(self, name: str) -> None: ...


def _pack(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def _unpack(b: bytes) -> array:
    a = array("f")
    a.frombytes(b)
    return a


class MemoryStore:
    """Vectors as float32 blobs in SQLite, one connection per call."""

    async def _dim(self, conn, name: str) -> int:
        r = await (await conn.execute("select dim from kb_vector_collections where name=?", (name,))).fetchone()
        if not r:
            raise VectorError(f"unknown vector collection {name}")
        return r["dim"]

    async def ensure_collection(self, name: str, dim: int) -> None:
        conn = await db.connect()
        try:
            r = await (await conn.execute("select dim from kb_vector_collections where name=?", (name,))).fetchone()
            if r and r["dim"] != dim:
                raise VectorError(f"{name} already exists with dimension {r['dim']}, not {dim}")
            if not r:
                await conn.execute("insert or ignore into kb_vector_collections(name, dim) values(?,?)", (name, dim))
                await conn.commit()
        finally:
            await conn.close()

    async def upsert(self, name: str, points: list[Point]) -> None:
        conn = await db.connect()
        try:
            dim = await self._dim(conn, name)
            if any(len(p.vector) != dim for p in points):
                raise VectorError(f"{name} takes vectors of dimension {dim}")
            await conn.executemany(
                "insert into kb_vectors(collection, chunk_id, document_id, vec) values(?,?,?,?) "
                "on conflict(collection, chunk_id) do update set document_id=excluded.document_id, vec=excluded.vec",
                [(name, p.id, p.document_id, _pack(p.vector)) for p in points])
            await conn.commit()
        finally:
            await conn.close()

    async def delete(self, name: str, ids: list[str]) -> None:
        conn = await db.connect()
        try:
            await self._dim(conn, name)
            await conn.executemany("delete from kb_vectors where collection=? and chunk_id=?", [(name, i) for i in ids])
            await conn.commit()
        finally:
            await conn.close()

    async def delete_by_document(self, name: str, document_id: str) -> None:
        conn = await db.connect()
        try:
            await self._dim(conn, name)
            await conn.execute("delete from kb_vectors where collection=? and document_id=?", (name, document_id))
            await conn.commit()
        finally:
            await conn.close()

    async def search(self, name: str, vector: list[float], limit: int) -> list[tuple[str, float]]:
        conn = await db.connect()
        try:
            await self._dim(conn, name)
            rows = await (await conn.execute("select chunk_id, vec from kb_vectors where collection=?", (name,))).fetchall()
        finally:
            await conn.close()
        qn = math.sqrt(sum(x * x for x in vector)) or 1.0
        scored = []
        for r in rows:
            v = _unpack(r["vec"])
            vn = math.sqrt(sum(x * x for x in v)) or 1.0
            scored.append((r["chunk_id"], sum(a * b for a, b in zip(vector, v)) / (qn * vn)))
        scored.sort(key=lambda s: -s[1])
        return scored[:limit]

    async def count(self, name: str) -> int:
        conn = await db.connect()
        try:
            await self._dim(conn, name)
            return (await (await conn.execute("select count(*) from kb_vectors where collection=?", (name,))).fetchone())[0]
        finally:
            await conn.close()

    async def drop(self, name: str) -> None:
        conn = await db.connect()
        try:
            await conn.execute("delete from kb_vectors where collection=?", (name,))
            await conn.execute("delete from kb_vector_collections where name=?", (name,))
            await conn.commit()
        finally:
            await conn.close()


class QdrantStore:
    def __init__(self, url: str | None = None, client=None, payload_index: bool = True):
        self._url, self._client, self._payload_index = url, client, payload_index

    def _c(self):
        if self._client is None:
            from qdrant_client import AsyncQdrantClient
            self._client = AsyncQdrantClient(url=self._url or config.qdrant_url(), prefer_grpc=False,
                                             timeout=30, check_compatibility=False)
        return self._client

    async def _run(self, make, *, known: str | None = None):
        """Run one client call; any client or transport failure becomes a VectorError."""
        try:
            if known is not None and not await self._c().collection_exists(known):
                raise VectorError(f"unknown vector collection {known}")
            return await make(self._c())
        except VectorError:
            raise
        except Exception as e:  # noqa: BLE001 - the client raises many unrelated types
            raise VectorError(f"vector store unavailable: {type(e).__name__}") from None

    async def ensure_collection(self, name: str, dim: int) -> None:
        from qdrant_client import models

        async def make(c):
            if await c.collection_exists(name):
                have = (await c.get_collection(name)).config.params.vectors.size
                if have != dim:
                    raise VectorError(f"{name} already exists with dimension {have}, not {dim}")
                return
            await c.create_collection(name, vectors_config=models.VectorParams(
                size=dim, distance=models.Distance.COSINE, on_disk=True))
            if self._payload_index:
                await c.create_payload_index(name, "document_id", models.PayloadSchemaType.KEYWORD)
        await self._run(make)

    async def upsert(self, name: str, points: list[Point]) -> None:
        from qdrant_client import models
        structs = [models.PointStruct(id=str(uuid.UUID(p.id)), vector=p.vector,
                                      payload={"document_id": p.document_id}) for p in points]
        await self._run(lambda c: c.upsert(name, structs, wait=True), known=name)

    async def delete(self, name: str, ids: list[str]) -> None:
        from qdrant_client import models
        sel = models.PointIdsList(points=[str(uuid.UUID(i)) for i in ids])
        await self._run(lambda c: c.delete(name, sel, wait=True), known=name)

    async def delete_by_document(self, name: str, document_id: str) -> None:
        from qdrant_client import models
        sel = models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(
            key="document_id", match=models.MatchValue(value=document_id))]))
        await self._run(lambda c: c.delete(name, sel, wait=True), known=name)

    async def search(self, name: str, vector: list[float], limit: int) -> list[tuple[str, float]]:
        res = await self._run(lambda c: c.query_points(name, query=vector, limit=limit, with_payload=False),
                              known=name)
        return [(uuid.UUID(str(p.id)).hex, float(p.score)) for p in res.points]

    async def count(self, name: str) -> int:
        return (await self._run(lambda c: c.count(name, exact=True), known=name)).count

    async def drop(self, name: str) -> None:
        async def make(c):
            if await c.collection_exists(name):
                await c.delete_collection(name)
        await self._run(make)


def get_store() -> VectorStore:
    return MemoryStore() if config.vector_backend() == "memory" else QdrantStore()
