from pathlib import Path

import pytest

from hunterai.kb import search
from hunterai.kb.embed import EmbedError
from hunterai.repo import kb
from tests.kb_samples import StubStore, ingest_texts, make_collection


def test_to_match_any():
    assert search.to_match_any('how to bypass "WAF"') == '"how" OR "to" OR "bypass" OR """WAF"""'
    assert search.to_match_any("  ") == "" == search.to_match_any('"') and len(search.to_match_any(" ".join(f"t{i}" for i in range(50))).split(" OR ")) == 32


async def test_rrf_fuses_dense_and_keyword_ranks(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta", "d2.txt": "gamma delta", "d3.txt": "alpha epsilon"})
    cid = lambda n: kb.chunk_id(docs[n].id, 0)  # noqa: E731
    stub = StubStore({kb.vector_name(col.id): [(cid("d2.txt"), 0.9), (cid("d1.txt"), 0.8), ("f" * 32, 0.7)]})
    hits = await search.search(conn, [col.id], "alpha", store=stub)
    assert [Path(h.source_uri).name for h in hits] == ["d1.txt", "d2.txt", "d3.txt"]         # the orphan id is dropped
    assert [(h.dense_rank, h.keyword_rank) for h in hits] == [(2, 1), (1, None), (None, 2)]
    assert [h.score for h in hits] == pytest.approx([1 / 62 + 1 / 61, 1 / 61, 1 / 62]) and (hits[0].text, hits[0].ordinal) == ("alpha beta", 0)


async def test_real_stores_put_the_relevant_chunk_first(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, {"sqli.md": "union select sqli payload for mysql", "xss.md": "script alert xss payload in html",
                                             "heap.md": "kernel heap spray grooming technique"})
    hits = await search.search(conn, [col.id], "mysql union sqli", limit=2)
    assert len(hits) == 2 and hits[0].source_uri.endswith("sqli.md") and hits[0].dense_rank == hits[0].keyword_rank == 1


@pytest.mark.parametrize("q", ['"unterminated', 'a AND', 'foo*bar', '-x', 'col:val', '()', 'NEAR(a b)', 'a OR', '^x', '{a}', 'a\x00b'])
async def test_query_with_special_characters_never_raises(conn, col, tmp_path, q):
    await ingest_texts(conn, col, tmp_path, {"a.txt": "alpha beta"})
    assert isinstance(await search.search(conn, [col.id], q), list)


async def test_blank_query_or_no_collection_embeds_nothing(conn, col, embed_spy):
    assert await search.search(conn, [col.id], "  ") == [] == await search.search(conn, [], "x") == await search.search(conn, ["nope"], "x")
    assert embed_spy.batches == []


async def test_only_ok_documents_of_the_selected_collections(conn, col, tmp_path):
    other = await make_collection(conn, "other")
    await ingest_texts(conn, other, tmp_path, {"o.txt": "alpha secret"})
    docs = await ingest_texts(conn, col, tmp_path, {"a.txt": "alpha one", "b.txt": "alpha two"})
    await conn.execute("update kb_documents set status='processing' where id=?", (docs["b.txt"].id,)); await conn.commit()
    hits = await search.search(conn, [col.id], "alpha")
    assert [Path(h.source_uri).name for h in hits] == ["a.txt"]


async def test_one_embed_call_per_distinct_embedder(conn, col, tmp_path, embed_spy):
    other = await make_collection(conn, "other")
    await ingest_texts(conn, col, tmp_path, {"a.txt": "alpha"}); await ingest_texts(conn, other, tmp_path, {"b.txt": "alpha too"})
    before = len(embed_spy.batches)
    assert len(await search.search(conn, [col.id, other.id], "alpha")) == 2 and len(embed_spy.batches) == before + 1


async def test_embedder_failure_propagates(conn, col, tmp_path, embed_spy):
    await ingest_texts(conn, col, tmp_path, {"a.txt": "alpha"})
    embed_spy.fail = EmbedError("unavailable", "down")
    with pytest.raises(EmbedError):
        await search.search(conn, [col.id], "alpha")


async def test_limit_is_applied_after_fusion(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, {f"d{i}.txt": f"alpha note number {i}" for i in range(12)})
    hits = await search.search(conn, [col.id], "alpha note", limit=5)
    assert len(hits) == 5 and [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
