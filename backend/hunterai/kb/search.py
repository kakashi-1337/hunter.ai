"""Hybrid search over knowledge collections: nearest vectors and keyword matches, fused by
reciprocal rank. Only chunks of documents that finished ingesting are ever returned."""
from __future__ import annotations

from ..repo import kb
from ..types import Hit
from . import embed
from .vectors import VectorStore, get_store

RRF_K = 60
CANDIDATES = 40
MAX_TERMS = 32


def to_match_any(q: str) -> str:
    """An FTS5 query that matches any of the words, with nothing in `q` read as syntax."""
    terms = []
    for term in q.replace("\0", "").split():
        if term.strip('"'):
            terms.append('"' + term.replace('"', '""') + '"')
    return " OR ".join(terms[:MAX_TERMS])


async def search(conn, collection_ids: list[str], query: str, limit: int = 8, *,
                 store: VectorStore | None = None, lock_timeout: float | None = 30) -> list[Hit]:
    query = (query or "").strip()
    cols = [c for c in [await kb.get_collection(conn, i) for i in dict.fromkeys(collection_ids)] if c]
    if not query or not cols:
        return []
    store = store or get_store()
    vectors: dict[str, list[float]] = {}
    for spec in dict.fromkeys(c.embedder for c in cols):
        vectors[spec] = (await embed.get(spec, lock_timeout).embed([query]))[0]
    dense: dict[str, int] = {}
    for c in cols:
        for rank, (chunk_id, _) in enumerate(await store.search(kb.vector_name(c.id), vectors[c.embedder], CANDIDATES), 1):
            dense.setdefault(chunk_id, rank)
    match = to_match_any(query)
    keyword = {cid: rank for rank, cid in enumerate(
        await kb.keyword_ids(conn, [c.id for c in cols], match, CANDIDATES) if match else [], 1)}
    scored = []
    for cid in {*dense, *keyword}:
        d, k = dense.get(cid), keyword.get(cid)
        score = (1 / (RRF_K + d) if d else 0) + (1 / (RRF_K + k) if k else 0)
        scored.append((-score, d if d is not None else 10**9, cid, d, k))
    scored.sort()
    found = await kb.chunks_by_id(conn, [s[2] for s in scored])
    hits = []
    for neg, _, cid, d, k in scored:
        if cid not in found:
            continue               # an orphan vector, or a document that is not ok
        chunk, doc = found[cid]
        hits.append(Hit(chunk_id=cid, document_id=doc.id, collection_id=doc.collection_id,
                        source_uri=doc.source_uri, ordinal=chunk.ordinal, text=chunk.text,
                        line_start=chunk.line_start, line_end=chunk.line_end, score=-neg,
                        dense_rank=d, keyword_rank=k))
        if len(hits) >= limit:
            break
    return hits
