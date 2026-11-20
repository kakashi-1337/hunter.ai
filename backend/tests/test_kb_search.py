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


# ---------------------------------------------------------------- an identifier is found by its exact spelling

def chunk(docs, name):
    return kb.chunk_id(docs[name].id, 0)


def dense(col, docs, names):
    """A vector half that brings these documents, in this order."""
    return StubStore({kb.vector_name(col.id): [(chunk(docs, n), 0.9 - i / 100) for i, n in enumerate(names)]})


def test_the_identifiers_of_a_query():
    assert search.identifiers("where is user_name checked in /api/v1/users, and getUserById?") == ["user_name", "/api/v1/users", "getUserById"]
    assert search.identifiers("Is cve-2024-1234 the same as CVE-2024-1234?") == ["cve-2024-1234", "CVE-2024-1234"]
    # What stands inside a longer identifier is not asked a second time.
    assert search.identifiers("GET /api/v1/user_name/getAll") == ["/api/v1/user_name/getAll"]
    # Ordinary words with a hyphen, a slash or a dot, and names that only begin with a capital, are no identifiers.
    assert search.identifiers("state-of-the-art and/or read/write in JavaScript 3.14, e.g. x_y") == []
    assert search.identifiers("") == []


async def test_an_identifier_made_of_ordinary_words_is_found_by_its_spelling(conn, col, tmp_path):
    """`wait_for` is "wait for" to the keyword index: every passage with those two words matches, and the one that
    has the identifier is not among the 40 it brings."""
    texts = {f"n{i:02}.txt": f"Then wait for the page to load, and wait for the next one. Note {i}." for i in range(50)}
    texts["api.txt"] = "Call wait_for with a selector before reading the table."
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "wait_for", limit=40, store=dense(col, docs, [f"n{i:02}.txt" for i in range(40)]))
    assert hits[0].source_uri.endswith("api.txt") and hits[0].exact
    assert not any(h.exact for h in hits[1:]) and len(hits) == 40


async def test_the_capitals_of_an_identifier_count(conn, col, tmp_path):
    texts = {f"n{i:02}.txt": f"The jquery upgrade is planned, and jquery plugins follow. Note {i}." for i in range(50)}
    texts["tracks.txt"] = "Stairway to Heaven, jQuery, 4.5 stars"
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "jQuery", limit=40, store=dense(col, docs, [f"n{i:02}.txt" for i in range(40)]))
    assert hits[0].source_uri.endswith("tracks.txt") and hits[0].exact


async def test_asked_in_another_case_an_identifier_is_still_found(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, {"cve.txt": "The fix for CVE-2024-1234 landed in release 2.3.1.",
                                                    "other.txt": "Build 2024 produced 1234 warnings about a cve scanner."})
    hits = await search.search(conn, [col.id], "cve-2024-1234", store=dense(col, docs, ["other.txt"]))
    assert hits[0].source_uri.endswith("cve.txt") and hits[0].exact and not hits[1].exact


async def test_an_identifier_inside_a_question_puts_its_passage_first(conn, col, tmp_path):
    """Fused by rank, a passage both halves found stands before one that is first for the keywords alone, so the
    one passage with the identifier stood behind every passage that shares the question's ordinary words."""
    texts = {f"n{i:02}.txt": f"An attacker can read account data of another user on the server. Note {i}." for i in range(45)}
    texts["cve.txt"] = "The fix for CVE-2024-1234 landed in release 2.3.1."
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "how can an attacker use CVE-2024-1234 to read account data?",
                               store=dense(col, docs, [f"n{i:02}.txt" for i in range(40)]))
    assert hits[0].source_uri.endswith("cve.txt") and hits[0].exact and hits[0].keyword_rank is not None
    assert hits[0].score > hits[1].score            # one order: the list is still sorted by its score


async def test_a_word_that_stands_in_many_passages_is_no_identifier_of_the_collection(conn, col, tmp_path):
    texts = {f"n{i}.txt": f"The user_name field is checked here. Note {i}." for i in range(search.RARE + 1)}
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "user_name", store=dense(col, docs, ["n0.txt"]))
    assert len(hits) == search.RARE + 1 and not any(h.exact for h in hits)
    assert hits[0].source_uri.endswith("n0.txt")    # the order the two halves give, as before


async def test_a_question_without_an_identifier_is_searched_as_before(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta", "d2.txt": "gamma delta", "d3.txt": "alpha epsilon"})
    hits = await search.search(conn, [col.id], "alpha", store=dense(col, docs, ["d2.txt", "d1.txt"]))
    assert [Path(h.source_uri).name for h in hits] == ["d1.txt", "d2.txt", "d3.txt"] and not any(h.exact for h in hits)
    assert [h.score for h in hits] == pytest.approx([1 / 62 + 1 / 61, 1 / 61, 1 / 62])


async def test_a_paste_full_of_identifiers_puts_nothing_first(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, {"a.txt": "user_name is checked", "b.txt": "nothing of it"})
    paste = "user_name " + " ".join(f"param_{i}" for i in range(search.MAX_IDENTIFIERS))
    hits = await search.search(conn, [col.id], paste, store=dense(col, docs, ["b.txt"]))
    assert hits and not any(h.exact for h in hits)


async def test_at_most_a_few_passages_stand_first_and_each_identifier_gets_its_turn(conn, col, tmp_path):
    texts = {f"u{i}.txt": f"user_name appears in note {i}" for i in range(3)} | {"t.txt": "team_id appears once"} | {"z.txt": "nothing of it"}
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "user_name and team_id", store=dense(col, docs, ["z.txt"]))
    first = [Path(h.source_uri).name for h in hits if h.exact]
    assert len(first) == search.MAX_EXACT == 3 and "t.txt" in first        # not three of the first identifier alone


async def test_the_spelling_is_looked_for_in_the_collections_asked_and_in_finished_documents_only(conn, col, tmp_path):
    other = await make_collection(conn, "other")
    await ingest_texts(conn, other, tmp_path, {"elsewhere.txt": "wait_for lives in another collection"})
    docs = await ingest_texts(conn, col, tmp_path, {"here.txt": "wait_for is here", "gone.txt": "wait_for was here"})
    await conn.execute("update kb_documents set status='failed' where id=?", (docs["gone.txt"].id,))
    await conn.commit()
    hits = await search.search(conn, [col.id], "wait_for", store=StubStore({}))
    assert [Path(h.source_uri).name for h in hits] == ["here.txt"] and hits[0].exact


async def test_of_the_passages_with_an_identifier_those_the_two_halves_rank_best_stand_first(conn, col, tmp_path):
    """Five passages hold it and three are put first: which three is not left to the order they were indexed in."""
    texts = {f"u{i}.txt": f"user_name appears in note {i}" for i in range(search.RARE)} | {"z.txt": "nothing of it"}
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "user_name", store=dense(col, docs, ["u4.txt", "z.txt", "u3.txt"]))
    first = [Path(h.source_uri).name for h in hits if h.exact]
    assert len(first) == search.MAX_EXACT and first[:2] == ["u4.txt", "u3.txt"]
