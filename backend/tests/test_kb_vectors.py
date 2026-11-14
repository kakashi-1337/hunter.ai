import pytest

from hunterai.kb import vectors
from hunterai.kb.vectors import Point, VectorError

A, B, C = "a" * 32, "b" * 32, "c" * 32


@pytest.fixture(params=["memory", "qdrant-local"])
async def store(request, conn):
    if request.param == "memory":
        yield vectors.MemoryStore(); return
    from qdrant_client import AsyncQdrantClient
    client = AsyncQdrantClient(location=":memory:")
    yield vectors.QdrantStore(client=client, payload_index=False)      # local mode warns on payload indexes
    await client.close()


async def fill(store):
    await store.ensure_collection("kb_t", 3)
    await store.upsert("kb_t", [Point(A, "d1", [1, 0, 0]), Point(B, "d1", [0.9, 0.1, 0]), Point(C, "d2", [0, 1, 0])])


async def test_search_orders_by_cosine(store):
    await fill(store)
    got = await store.search("kb_t", [1, 0, 0], 2)
    assert [g[0] for g in got] == [A, B] and got[0][1] == pytest.approx(1.0, abs=1e-4) and got[0][1] > got[1][1]


async def test_upsert_same_id_overwrites(store):
    await fill(store); await store.upsert("kb_t", [Point(A, "d1", [0, 0, 1])])
    assert await store.count("kb_t") == 3 and (await store.search("kb_t", [0, 0, 1], 1))[0][0] == A


async def test_delete_and_delete_by_document(store):
    await fill(store); await store.delete("kb_t", [C])
    assert await store.count("kb_t") == 2
    await store.delete_by_document("kb_t", "d1"); await store.delete_by_document("kb_t", "nobody")
    assert await store.count("kb_t") == 0


async def test_ensure_is_idempotent_and_rejects_another_dimension(store):
    await fill(store); await store.ensure_collection("kb_t", 3)
    assert await store.count("kb_t") == 3
    with pytest.raises(VectorError):
        await store.ensure_collection("kb_t", 4)


async def test_drop_and_unknown_collection(store):
    await fill(store); await store.drop("kb_t"); await store.drop("kb_t")
    for call in (store.count("kb_t"), store.search("kb_t", [1, 0, 0], 1), store.upsert("kb_t", [Point(A, "d", [1, 0, 0])])):
        with pytest.raises(VectorError):
            await call


async def test_memory_store_is_shared_between_instances(conn):
    await fill(vectors.MemoryStore())
    assert await vectors.MemoryStore().count("kb_t") == 3


def test_get_store_selection(monkeypatch):
    assert isinstance(vectors.get_store(), vectors.MemoryStore)
    monkeypatch.delenv("HUNTERAI_VECTOR")
    assert isinstance(vectors.get_store(), vectors.QdrantStore)
    monkeypatch.setenv("HUNTERAI_VECTOR", "bogus")
    with pytest.raises(ValueError):
        vectors.get_store()


async def test_unreachable_qdrant_is_a_vector_error():
    with pytest.raises(VectorError, match="unavailable"):
        await vectors.QdrantStore(url="http://127.0.0.1:9").count("kb_t")
