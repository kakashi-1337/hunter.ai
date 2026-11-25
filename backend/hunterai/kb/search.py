"""Hybrid search over knowledge collections: nearest vectors and keyword matches, fused by
reciprocal rank. Only chunks of documents that finished ingesting are ever returned.

Before the fused list stand the passages that hold an identifier of the query in its exact spelling (a CVE number,
a path, a snake_case or camelCase name). The two halves are weak at exactly these, each in its own way:

- to the keyword index `wait_for` is the two words "wait for", and `jQuery` is "jquery". Every passage with the
  ordinary words matches, and the one that has the identifier is not among the 40 the index brings;
- fused by rank, a passage both halves found stands before one that is first for the keywords alone (with k=60 and
  two lists of 40, the worst of the first kind scores 2/100 and the best of the second 1/61). So inside a question,
  the one passage with the identifier stands behind every passage that shares the question's ordinary words.

An identifier that stands in more than RARE passages is an ordinary word of that collection and puts nothing
first, and a query that names more than MAX_IDENTIFIERS is a paste, not a question. What this is for was measured
with scripts/rag-test.sh on a real collection before it was added: of 30 identifiers asked on their own, 9 stood
first and 5 were not found at all (in_progress, result_type, form_input, wait_for, jQuery).

Behind those stands the first passage of each half: the nearest by meaning, then the best by its words. Fused by
rank alone they did not lead, for the reason given above: a passage both halves have anywhere in their 40 stands
before the one a single half has first, so the nearest passage of all could stand seventh behind six that share a
few words with the question, and a message has room for three passages or so. What this is for was counted before
it was added, on the kept test of a collection of 606,939 passages (45 questions written from its passages, the
search repeated from its two lists on): the right passage stood first for 18 of them instead of 15, and was in
the message for 29 instead of 26. A smaller k (5 or 2), or the better of a passage's two places, brought 16 and 28;
the two firsts put behind the fused first, 15 and 29.

The keyword half needs neither the embedding model nor the vector store, so it can answer when the other half cannot
be asked: `or_keywords` is the search for a reader that is better off with half an answer than with none."""
from __future__ import annotations

import re
from itertools import zip_longest

from ..repo import kb
from ..types import Hit
from . import embed
from .embed import EmbedError
from .vectors import VectorError, VectorStore, get_store

RRF_K = 60
CANDIDATES = 40
MAX_TERMS = 32
RARE = 5                    # passages: an identifier that stands in more is an ordinary word of the collection
MAX_EXACT = 3               # passages put first for an exact spelling: about what a message has room for
MAX_IDENTIFIERS = 6         # of one query: one that names more is a paste, and what it names decides nothing
MIN_IDENTIFIER = 4          # characters
EXACT_SCORE = 1.0           # added for an exact spelling. A fused score stays under 0.04, so the list keeps one order
FIRST_OF_VECTORS = 0.6      # added for the passage the vector half has first, and
FIRST_OF_KEYWORDS = 0.5     # for the one the keyword half has first: over every fused score, under an exact spelling

# What is an identifier here. Narrow on purpose: a passage put first for a word that was no identifier takes one of
# the three places a message has. An ordinary compound ("state-of-the-art", "and/or", "JavaScript", "3.14") is none.
IDENTIFIER_FORMS = (
    re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I),
    re.compile(r"(?<![\w/.-])/(?:[A-Za-z0-9_.~-]+/)+[A-Za-z0-9_~-][A-Za-z0-9_.~-]*"),      # a path of two parts or more
    re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"),                             # snake_case
    re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b"),                                          # camelCase
)


def identifiers(query: str) -> list[str]:
    """The identifiers a query names, in its order, each once. What stands inside a longer one is not listed again."""
    spans = sorted({(m.start(), m.end()) for form in IDENTIFIER_FORMS for m in form.finditer(query)},
                   key=lambda s: (s[0], -s[1]))
    out: list[str] = []
    end = 0
    for a, b in spans:
        if b <= end:
            continue                    # inside the one before it
        end = b
        word = query[a:b]
        if len(word) >= MIN_IDENTIFIER and word not in out:
            out.append(word)
    return out


async def exact(conn, collection_ids: list[str], query: str) -> list[list[str]]:
    """For each identifier of the query that is one in these collections, the chunks that hold it in its exact
    spelling (RARE at most each). Asked as it is written first; only when no passage has it so, in any case
    (someone types cve-2024-1234)."""
    named = identifiers(query)
    if not named or len(named) > MAX_IDENTIFIERS:
        return []
    found: list[list[str]] = []
    for word in named:
        phrase = '"' + word.replace('"', '""') + '"'
        ids = await kb.exact_ids(conn, collection_ids, phrase, word, RARE + 1)
        if not ids:
            ids = await kb.exact_ids(conn, collection_ids, phrase, word, RARE + 1, any_case=True)
        if 0 < len(ids) <= RARE:
            found.append(ids)
    return found


def put_first(found: list[list[str]], fused: dict[str, float]) -> list[str]:
    """The chunks that stand before the fused list, MAX_EXACT at most: each identifier in its turn, and of the
    passages that hold one, those the two halves rank best."""
    ranked = [sorted(ids, key=lambda cid: -fused.get(cid, 0.0)) for ids in found]
    out: list[str] = []
    for turn in zip_longest(*ranked):
        out += [cid for cid in turn if cid is not None and cid not in out]
    return out[:MAX_EXACT]


def leading(dense: dict[str, int], nearness: dict[str, float], keyword: dict[str, int], found,
            spelled: list[str]) -> dict[str, tuple[str, float]]:
    """The first passage of each half that still has its passage: which half it leads for, and what that adds to
    its score. One for the vectors, also when several collections are asked and each has a nearest of its own (the
    nearest of those; their numbers compare only where the collections share an embedding model, and the first
    collection's stands where they do not say). A passage that is first in both halves leads once, for the vectors.
    One that an exact spelling already put first is none of these, and no other passage leads in its place."""
    out: dict[str, tuple[str, float]] = {}
    first = min((cid for cid in dense if cid in found), key=lambda cid: (dense[cid], -nearness.get(cid, 0.0)), default=None)
    if first is not None and first not in spelled:
        out[first] = ("vectors", FIRST_OF_VECTORS)
    first = min((cid for cid in keyword if cid in found), key=keyword.__getitem__, default=None)
    if first is not None and first not in spelled:
        out.setdefault(first, ("keywords", FIRST_OF_KEYWORDS))
    return out


def to_match_any(q: str) -> str:
    """An FTS5 query that matches any of the words, with nothing in `q` read as syntax."""
    terms = []
    for term in q.replace("\0", "").split():
        if term.strip('"'):
            terms.append('"' + term.replace('"', '""') + '"')
    return " OR ".join(terms[:MAX_TERMS])


async def search(conn, collection_ids: list[str], query: str, limit: int = 8, *,
                 store: VectorStore | None = None, lock_timeout: float | None = 30, dense: bool = True) -> list[Hit]:
    """`dense` False: the keyword half and the exact spellings alone. Nothing is embedded and no vector store is asked."""
    query = (query or "").strip()
    cols = [c for c in [await kb.get_collection(conn, i) for i in dict.fromkeys(collection_ids)] if c]
    if not query or not cols:
        return []
    by_vector: dict[str, int] = {}
    nearness: dict[str, float] = {}
    if dense:
        store = store or get_store()
        vectors: dict[str, list[float]] = {}
        for spec in dict.fromkeys(c.embedder for c in cols):
            vectors[spec] = (await embed.get(spec, lock_timeout).embed([query]))[0]
        for c in cols:
            for rank, (chunk_id, near) in enumerate(await store.search(kb.vector_name(c.id), vectors[c.embedder], CANDIDATES), 1):
                if chunk_id not in by_vector:
                    by_vector[chunk_id], nearness[chunk_id] = rank, near
    match = to_match_any(query)
    keyword = {cid: rank for rank, cid in enumerate(
        await kb.keyword_ids(conn, [c.id for c in cols], match, CANDIDATES) if match else [], 1)}
    fused = {cid: (1 / (RRF_K + by_vector[cid]) if cid in by_vector else 0) + (1 / (RRF_K + keyword[cid]) if cid in keyword else 0)
             for cid in {*by_vector, *keyword}}
    spelled = put_first(await exact(conn, [c.id for c in cols], query), fused)
    found = await kb.chunks_by_id(conn, list({*fused, *spelled}))
    leads = leading(by_vector, nearness, keyword, found, spelled)
    scored = []
    for cid in {*fused, *spelled}:
        d, k = by_vector.get(cid), keyword.get(cid)
        score = fused.get(cid, 0) + (EXACT_SCORE if cid in spelled else leads[cid][1] if cid in leads else 0)
        scored.append((-score, d if d is not None else 10**9, cid, d, k))
    scored.sort()
    hits = []
    for neg, _, cid, d, k in scored:
        if cid not in found:
            continue               # an orphan vector, or a document that is not ok
        chunk, doc = found[cid]
        hits.append(Hit(chunk_id=cid, document_id=doc.id, collection_id=doc.collection_id,
                        source_uri=doc.source_uri, ordinal=chunk.ordinal, text=chunk.text,
                        line_start=chunk.line_start, line_end=chunk.line_end, score=-neg,
                        dense_rank=d, keyword_rank=k, exact=cid in spelled,
                        first_of=leads[cid][0] if cid in leads else None, parts=doc.chunk_count))
        if len(hits) >= limit:
            break
    return hits


async def or_keywords(conn, collection_ids: list[str], query: str, limit: int = 8, *,
                      lock_timeout: float | None = 30) -> tuple[list[Hit], str | None]:
    """The search, for a reader that is better off with half an answer than with none: a model in the middle of
    a run, a message that is about to go out. When the vector half cannot be asked (the embedder is busy with a job
    or does not answer, the vector store is down or does not know the collection), the keyword half answers alone.
    Returns the hits, and why the vector half did not answer when it did not. A failure of anything else is raised
    as it is: only the vector half has a stand-in."""
    try:
        return await search(conn, collection_ids, query, limit, lock_timeout=lock_timeout), None
    except EmbedError as e:
        why = e.message
    except VectorError as e:
        why = str(e)
    return await search(conn, collection_ids, query, limit, dense=False), why
