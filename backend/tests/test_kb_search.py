from pathlib import Path

import pytest

from hunterai.kb import search
from hunterai.kb.embed import EmbedError
from hunterai.kb.vectors import VectorError
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
    # The orphan id is dropped. The first of the vectors and the first of the keywords lead, then what is left.
    assert [Path(h.source_uri).name for h in hits] == ["d2.txt", "d1.txt", "d3.txt"]
    assert [(h.dense_rank, h.keyword_rank) for h in hits] == [(1, None), (2, 1), (None, 2)]
    assert [h.score for h in hits] == pytest.approx([search.FIRST_OF_VECTORS + 1 / 61,
                                                     search.FIRST_OF_KEYWORDS + 1 / 62 + 1 / 61, 1 / 62])
    assert (hits[1].text, hits[1].ordinal) == ("alpha beta", 0)


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


async def test_a_question_without_an_identifier_is_ordered_by_the_two_halves_alone(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta", "d2.txt": "gamma delta", "d3.txt": "alpha epsilon"})
    hits = await search.search(conn, [col.id], "alpha", store=dense(col, docs, ["d2.txt", "d1.txt"]))
    assert [Path(h.source_uri).name for h in hits] == ["d2.txt", "d1.txt", "d3.txt"] and not any(h.exact for h in hits)
    assert [h.score for h in hits] == pytest.approx([search.FIRST_OF_VECTORS + 1 / 61,
                                                     search.FIRST_OF_KEYWORDS + 1 / 62 + 1 / 61, 1 / 62])


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


# ---------------------------------------------------------------- the first passage of each half stands first

def names(hits):
    return [Path(h.source_uri).name for h in hits]


async def test_the_first_passage_of_each_half_stands_before_what_both_halves_found_far_down(conn, col, tmp_path):
    """Fused by rank with k=60, a passage both halves have anywhere in their 40 stands before the passage one half
    has first (2/100 against 1/61): the nearest passage by meaning stood seventh behind six that shared words."""
    texts = {f"both{i}.txt": f"Notes on session tokens and cookies, part {i} of the series." for i in range(6)}
    texts["meaning.txt"] = "How a login stays valid between two requests."              # no word of the question
    texts["words.txt"] = "session tokens cookies: session tokens, cookies"              # the keyword half's first
    docs = await ingest_texts(conn, col, tmp_path, texts)
    store = dense(col, docs, ["meaning.txt", *(f"both{i}.txt" for i in range(6))])
    hits = await search.search(conn, [col.id], "session tokens cookies", store=store)
    assert names(hits[:2]) == ["meaning.txt", "words.txt"]
    assert [(h.dense_rank, h.keyword_rank) for h in hits[:2]] == [(1, None), (None, 1)]
    assert all(h.dense_rank and h.keyword_rank for h in hits[2:]) and len(hits) == 8
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)     # one order: still by its score
    assert hits[0].score == pytest.approx(search.FIRST_OF_VECTORS + 1 / 61)
    assert hits[1].score == pytest.approx(search.FIRST_OF_KEYWORDS + 1 / 61)
    assert [h.first_of for h in hits[:3]] == ["vectors", "keywords", None]             # each says why it leads


async def test_an_exact_spelling_stands_before_the_first_of_each_half(conn, col, tmp_path):
    texts = {f"n{i}.txt": f"An attacker can read account data of another user. Note {i}." for i in range(4)}
    texts |= {"cve.txt": "The fix for CVE-2024-1234 landed in release 2.3.1.",
              "meaning.txt": "How a login stays valid between two requests."}
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "can an attacker use CVE-2024-1234 to read account data",
                               store=dense(col, docs, ["meaning.txt", "n0.txt", "n1.txt"]))
    assert names(hits[:2]) == ["cve.txt", "meaning.txt"] and hits[0].exact and not hits[1].exact
    assert hits[0].score >= search.EXACT_SCORE > hits[1].score >= search.FIRST_OF_VECTORS
    assert (hits[0].first_of, hits[1].first_of) == (None, "vectors")
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


async def test_a_passage_with_an_exact_spelling_leads_for_that_alone_and_no_other_takes_its_place(conn, col, tmp_path):
    texts = {f"n{i}.txt": f"An attacker can read account data of another user. Note {i}." for i in range(4)}
    texts |= {"cve.txt": "The fix for CVE-2024-1234 landed in release 2.3.1.",
              "meaning.txt": "How a login stays valid between two requests."}
    docs = await ingest_texts(conn, col, tmp_path, texts)
    hits = await search.search(conn, [col.id], "can an attacker use CVE-2024-1234 to read account data",
                               store=dense(col, docs, ["cve.txt", "meaning.txt", "n0.txt"]))
    assert names(hits)[0] == "cve.txt" and hits[0].exact and hits[0].first_of is None
    assert hits[0].score < search.EXACT_SCORE + search.FIRST_OF_KEYWORDS         # its spelling put it first, nothing more
    assert not any(h.first_of == "vectors" for h in hits)                         # the second of the vectors does not lead


async def test_a_passage_that_is_first_in_both_halves_leads_once_and_alone(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, {"top.txt": "alpha beta alpha beta", "a.txt": "alpha and one more word",
                                                    "b.txt": "beta and two more words", "c.txt": "gamma"})
    hits = await search.search(conn, [col.id], "alpha beta", store=dense(col, docs, ["top.txt", "c.txt", "a.txt"]))
    assert names(hits)[0] == "top.txt" and hits[0].score == pytest.approx(search.FIRST_OF_VECTORS + 2 / 61)
    assert all(h.score < search.FIRST_OF_KEYWORDS for h in hits[1:]) and len(hits) == 4
    assert [h.first_of for h in hits] == ["vectors", None, None, None]


async def test_the_first_of_the_vectors_is_the_first_that_still_has_its_passage(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, {"a.txt": "alpha", "b.txt": "nothing of it", "c.txt": "alpha alpha gamma"})
    stub = StubStore({kb.vector_name(col.id): [("f" * 32, 0.95), (chunk(docs, "b.txt"), 0.9), (chunk(docs, "a.txt"), 0.8)]})
    hits = await search.search(conn, [col.id], "alpha", store=stub)
    assert names(hits)[0] == "b.txt" and (hits[0].dense_rank, hits[0].keyword_rank) == (2, None)
    assert hits[1].keyword_rank == 1 and hits[1].score >= search.FIRST_OF_KEYWORDS


async def test_of_several_collections_one_passage_leads_for_the_vectors_the_nearest(conn, col, tmp_path):
    """Each collection has a nearest passage of its own. One of them leads, not one for every collection: a message
    has room for three passages or so."""
    other = await make_collection(conn, "other")
    here = await ingest_texts(conn, col, tmp_path, {"far.txt": "nothing of it"})
    there = await ingest_texts(conn, other, tmp_path, {"near.txt": "nothing of it either", "words.txt": "alpha"})
    stub = StubStore({kb.vector_name(col.id): [(chunk(here, "far.txt"), 0.5)],
                      kb.vector_name(other.id): [(chunk(there, "near.txt"), 0.9)]})
    hits = await search.search(conn, [col.id, other.id], "alpha", store=stub)
    assert names(hits) == ["near.txt", "words.txt", "far.txt"]
    assert [h.score >= search.FIRST_OF_KEYWORDS for h in hits] == [True, True, False]



# ---- the keyword half alone, when the vector half cannot be asked

class DownStore:
    """A vector store that cannot be asked: Qdrant is not running, or does not know the collection."""

    def __init__(self, why: str = "vector store unavailable: ConnectError"):
        self.why = why

    async def search(self, name, vector, limit):
        raise VectorError(self.why)


async def test_the_keyword_half_alone_asks_no_embedder_and_no_vector_store(conn, col, tmp_path, embed_spy):
    await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta", "d2.txt": "gamma delta", "d3.txt": "alpha epsilon wait_for"})
    embed_spy.batches.clear()
    hits = await search.search(conn, [col.id], "alpha", store=DownStore(), dense=False)
    assert embed_spy.batches == [] and sorted(Path(h.source_uri).name for h in hits) == ["d1.txt", "d3.txt"]
    assert all(h.dense_rank is None and h.keyword_rank for h in hits) and hits[0].first_of == "keywords"
    spelled = await search.search(conn, [col.id], "how is wait_for used", store=DownStore(), dense=False)
    assert (Path(spelled[0].source_uri).name, spelled[0].exact) == ("d3.txt", True)       # an exact spelling still stands first


@pytest.mark.parametrize("failure", [EmbedError("unavailable", "embedder busy"),
                                     EmbedError("unavailable", "Ollama is not reachable (ConnectError)"),
                                     EmbedError("unavailable", "Ollama answered 500"),
                                     EmbedError("bad_request", "Ollama does not have model bge-m3. Run: ollama pull bge-m3")])
async def test_the_keyword_half_answers_with_the_reason_when_the_embedder_cannot_be_asked(conn, col, tmp_path, embed_spy, failure):
    await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta", "d2.txt": "gamma delta"})
    embed_spy.fail = failure
    with pytest.raises(EmbedError):
        await search.search(conn, [col.id], "alpha")                     # the search itself still says so
    hits, why = await search.or_keywords(conn, [col.id], "alpha")
    assert why == failure.message and [Path(h.source_uri).name for h in hits] == ["d1.txt"]


@pytest.mark.parametrize("why", ["vector store unavailable: ConnectError", "unknown vector collection kb_x"])
async def test_the_keyword_half_answers_with_the_reason_when_the_vector_store_cannot_be_asked(conn, col, tmp_path, monkeypatch, why):
    await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta", "d2.txt": "gamma delta"})
    monkeypatch.setattr(search, "get_store", lambda: DownStore(why))
    hits, said = await search.or_keywords(conn, [col.id], "alpha")
    assert said == why and [Path(h.source_uri).name for h in hits] == ["d1.txt"]


async def test_or_keywords_is_the_whole_search_when_both_halves_answer(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, {"sqli.md": "union select sqli payload for mysql", "xss.md": "script alert xss payload in html"})
    hits, why = await search.or_keywords(conn, [col.id], "mysql union sqli", 2)
    assert why is None and hits == await search.search(conn, [col.id], "mysql union sqli", 2) and hits[0].dense_rank == 1


async def test_or_keywords_does_not_hide_a_failure_of_the_keyword_half(conn, col, tmp_path, embed_spy, monkeypatch):
    """Only the vector half has a stand-in. What fails without it is not a search that half answered."""
    await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta"})
    embed_spy.fail = EmbedError("unavailable", "embedder busy")

    async def broken(*a, **kw):
        raise RuntimeError("the keyword index is gone")
    monkeypatch.setattr(kb, "keyword_ids", broken)
    with pytest.raises(RuntimeError, match="keyword index is gone"):
        await search.or_keywords(conn, [col.id], "alpha")


async def test_or_keywords_stands_in_only_for_a_half_that_cannot_be_asked(conn, col, tmp_path, monkeypatch):
    """A fault that is nobody's absence (a bug in a store, an embedder nobody knows) is not answered with half a search."""
    await ingest_texts(conn, col, tmp_path, {"d1.txt": "alpha beta"})

    class Buggy:
        async def search(self, name, vector, limit):
            raise RuntimeError("a bug in the store")
    monkeypatch.setattr(search, "get_store", lambda: Buggy())
    with pytest.raises(RuntimeError, match="a bug in the store"):
        await search.or_keywords(conn, [col.id], "alpha")


async def test_or_keywords_keeps_the_callers_limit_and_wait_in_both_cases(conn, col, tmp_path, embed_spy, monkeypatch):
    await ingest_texts(conn, col, tmp_path, {f"d{i}.txt": f"alpha note {i}" for i in range(4)})
    waits, real = [], search.embed.get

    def get(spec, lock_timeout=None, **kw):
        waits.append(lock_timeout)
        return real(spec, lock_timeout, **kw)
    monkeypatch.setattr(search.embed, "get", get)
    hits, why = await search.or_keywords(conn, [col.id], "alpha", 2, lock_timeout=7)
    assert (len(hits), why, waits) == (2, None, [7])
    embed_spy.fail = EmbedError("unavailable", "embedder busy")
    hits, why = await search.or_keywords(conn, [col.id], "alpha", 3, lock_timeout=7)
    assert (len(hits), why, waits) == (3, "embedder busy", [7, 7])          # the keyword half alone asks no embedder
