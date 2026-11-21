"""What the kept search test of a collection would count with another order of what the search already finds.

It changes nothing in the collection and nothing in the kept test. For everything the test asks it gets what the two
halves of the search bring (vectors, keywords), shows where the passage of each question stands in each half, and
counts the same test again: with other ways of putting the two lists together, with more room in the message, with
deeper lists, and without near copies. It writes one file of its own next to the kept test, the passages the halves
bring for each question, which rag_rerank.py reads.

From the backend folder:   uv run python - < rag_whatif.py        (WHATIF_COLLECTION: another collection than "RAG")

A diagnostic for one decision, not a part of HunterAI. It repeats what kb/search.py does from its two lists on, and
was checked against it: on three made-up collections its order for "as it is now" was the order search.search() gives
for every item, and its counts were the counts of ragtest.run()."""
import asyncio
import json
import os
import sys

from hunterai import config, db
from hunterai.kb import embed, ragtest, retrieve, search
from hunterai.kb.vectors import get_store
from hunterai.repo import kb
from hunterai.types import Hit

NAME = os.environ.get("WHATIF_COLLECTION", "RAG")
DEEP = 200                                  # passages each half is asked for here; the search itself asks for 40
C, TOP, ROOM, POOL = search.CANDIDATES, ragtest.TOP, ragtest.ROOM, ragtest.POOL
WORDS = ragtest.IN_WORDS                    # the kinds that are asked in a person's words
BANDS = ((1, 1), (2, 3), (4, 8), (9, 20), (21, POOL))


def rrf(k, absent=None):
    """Fused by rank, as the search does it. `absent`: the place a passage counts as in a half that did not bring it
    (None: that half gives it nothing, which is how it is now)."""
    def fuse(d, w):
        total = 0.0
        for rank in (d, w):
            rank = absent if rank is None else rank
            if rank is not None:
                total += 1 / (k + rank)
        return total
    return fuse


def better(d, w):
    """The better of a passage's two places decides, and the other place only breaks a tie."""
    ranks = sorted(r for r in (d, w) if r is not None)
    return 1 / ranks[0] + (1e-4 / ranks[1] if len(ranks) > 1 else 0.0)


K = search.RRF_K
VARIANTS = [    # what it is called, passages from each half, how they are fused, which halves, characters of room
    (f"as it is now ({C} from each half, k={K})", C, rrf(K), "both", ROOM),
    ("the vectors alone", C, rrf(K), "vectors", ROOM),
    ("the keywords alone", C, rrf(K), "keywords", ROOM),
    ("k=30", C, rrf(30), "both", ROOM),
    ("k=15", C, rrf(15), "both", ROOM),
    ("k=5", C, rrf(5), "both", ROOM),
    ("k=2", C, rrf(2), "both", ROOM),
    (f"k={K}, a half that did not bring it counts as place {C + 1}", C, rrf(K, absent=C + 1), "both", ROOM),
    ("the better of its two places decides", C, better, "both", ROOM),
    (f"100 from each half, k={K}", 100, rrf(K), "both", ROOM),
    (f"{DEEP} from each half, k={K}", DEEP, rrf(K), "both", ROOM),
    (f"{DEEP} from each half, k=15", DEEP, rrf(15), "both", ROOM),
    ("as it is now, with room for 9,000 characters", C, rrf(K), "both", 9000),
    ("as it is now, with room for 12,000 characters", C, rrf(K), "both", 12000),
    ("as it is now, a near copy of a passage above it left out", C, rrf(K), "both", -ROOM),      # a room below 0: without copies
]


def ordered(dense, keyword, pins, found, fuse, limit):
    """The order the search would give: search.search() from its two lists on, with `fuse` in the place of its sum."""
    fused = {cid: fuse(dense.get(cid), keyword.get(cid)) for cid in {*dense, *keyword}}
    spelled = search.put_first(pins, fused)
    top = max(fused.values(), default=0.0)
    first = search.EXACT_SCORE if top < search.EXACT_SCORE else 4 * top + 1     # an exact spelling stands first, whatever the sum
    scored = sorted((-(fused.get(cid, 0) + (first if cid in spelled else 0)), dense.get(cid, 10**9), cid)
                    for cid in {*fused, *spelled})
    return [cid for _, _, cid in scored if cid in found][:limit], spelled


def cost(hit):
    """Characters a passage takes in a message: retrieve.pick() counts it so."""
    return len(retrieve._header(1, hit.source_uri, hit.line_start, hit.line_end, hit.text)) + len(hit.text) + 3


class Asked:
    """One thing the test asks, with what each half of the search brings for it."""

    def __init__(self, item, near, deep, words, pins, found):
        self.item, self.kind, self.own = item, item["kind"], item["chunk_id"]
        self.near, self.deep, self.words, self.pins, self.found = near, deep, words, pins, found
        self.hits = {cid: Hit(chunk_id=cid, document_id=doc.id, collection_id=doc.collection_id, source_uri=doc.source_uri,
                              ordinal=chunk.ordinal, text=chunk.text, line_start=chunk.line_start, line_end=chunk.line_end,
                              score=0.0) for cid, (chunk, doc) in found.items()}
        self.right = found[self.own][0].text if self.own in found else None
        self._shares = {}

    def lists(self, depth, halves="both"):
        dense = self.near if depth == C else self.deep[:depth]          # the 40 are the ones the search itself gets
        return ({cid: rank for rank, cid in enumerate(dense, 1)} if halves != "keywords" else {},
                {cid: rank for rank, cid in enumerate(self.words[:depth], 1)} if halves != "vectors" else {})

    def answers(self, cid):
        """Whether this passage answers it: its own passage, or for a question a twin of that one."""
        if cid == self.own:
            return True
        if self.kind not in WORDS or self.right is None:
            return False
        return self.share(self.own, cid) >= ragtest.TWIN

    def share(self, cid, other):
        key = (cid, other)
        if key not in self._shares:
            self._shares[key] = ragtest.shared(self.hits[cid].text, self.hits[other].text)
        return self._shares[key]

    def without_copies(self, order):
        """The order without the passages whose wording a passage above them already holds (TWIN of it or more)."""
        kept = []
        for cid in order:
            if not any(self.share(cid, above) >= ragtest.TWIN for above in kept):
                kept.append(cid)
        return kept

    def judge(self, order, room):
        """(the place of the best passage that answers it, whether one that answers it is in the message), or None:
        counted as ragtest.run() counts. A room below 0: that room, and near copies are left out first."""
        if room < 0:
            order, room = self.without_copies(order), -room
        sent = {s.chunk_id for s in retrieve.pick([self.hits[cid] for cid in order[:TOP]], room)}
        if order and order[0] == self.own:
            return 1, self.own in sent
        good = [place for place, cid in enumerate(order, 1) if self.answers(cid)]
        return (min(good), any(order[place - 1] in sent for place in good)) if good else None


async def gather(conn, col, store, embedder, item):
    query = (item["query"] or "").strip()
    vector = (await embedder.embed([query]))[0]
    name = kb.vector_name(col.id)
    near = [cid for cid, _ in await store.search(name, vector, C)]
    deep = [cid for cid, _ in await store.search(name, vector, DEEP)]
    match = search.to_match_any(query)
    words = await kb.keyword_ids(conn, [col.id], match, DEEP) if match else []
    pins = await search.exact(conn, [col.id], query)
    ids = list({*near, *deep, *words, *(cid for group in pins for cid in group), item["chunk_id"]})
    return Asked(item, near, deep, words, pins, await kb.chunks_by_id(conn, ids))


def place_in(cid, ids):
    return ids.index(cid) + 1 if cid in ids else None


def show(n, missing="-"):
    return missing if n is None else str(n)


async def main():
    conn = await db.connect()
    embedder = None
    try:
        col = next((c for c in await kb.list_collections(conn) if c.name == NAME), None)
        if col is None:
            print(f'There is no collection named "{NAME}".')
            return 1
        path = config.home() / "rag-tests" / f"{col.id}.json"
        if not path.exists():
            print(f'"{NAME}" has no kept test yet. Build one first: scripts/rag-test.sh "{NAME}"')
            return 1
        test = json.loads(path.read_text())["test"]
        print(f'Collection "{col.name}", embedding model {col.embedder}. {len(test["items"])} things are asked, the questions '
              f'written by {test.get("written_by")}. Nothing in the collection or in the kept test is changed by this.')
        store, embedder = get_store(), embed.get(col.embedder, None, stays_loaded=True)
        asked, gone = [], 0
        for n, item in enumerate(test["items"], 1):
            if n % 10 == 0 or n == len(test["items"]):
                print(f"  asking {n} of {len(test['items'])}", file=sys.stderr, flush=True)
            one = await gather(conn, col, store, embedder, item)
            if one.right is None:
                gone += 1               # its passage is no longer in the collection: not asked about, as in the test
                continue
            asked.append(one)
    except embed.EmbedError as e:
        print(f"The embedding model could not be asked ({e.message}). Is Ollama running?")
        return 1
    finally:
        if embedder is not None:
            await embedder.release()
        await conn.close()

    kinds = [k for k in ragtest.KINDS if any(a.kind == k for a in asked)]
    if gone:
        print(f"{gone} of them {'is about a passage' if gone == 1 else 'are about passages'} the collection no longer holds: "
              "left out, as the test leaves them out.")

    # ---- where each question's passage stands now
    now = {}
    for a in asked:
        dense, keyword = a.lists(C)
        order, spelled = ordered(dense, keyword, a.pins, a.found, rrf(K), POOL)
        now[id(a)] = (order, spelled, dense, keyword, a.judge(order, ROOM))
    print(f"\n1. Where the passage of each question stands now. place: in the order the search gives. vectors, keywords: its"
          f"\n   place in each half's own list of {C} (-: that half did not bring it). In brackets, for a passage that is not"
          f"\n   among the {POOL}: its place when the half is asked for {DEEP} (-: not among those either; {C} or less: the half"
          f"\n   did bring it, and it fell out when the two lists were put together and cut to {POOL}).")
    for kind in kinds:
        if kind not in WORDS:
            continue
        rows = []
        for a in (a for a in asked if a.kind == kind):
            order, _, dense, keyword, got = now[id(a)]
            query = " ".join(a.item["query"].split())
            query = query if len(query) <= 86 else query[:83] + "..."
            if got:
                cid = order[got[0] - 1]
                mark = "" if cid == a.own else "  [a twin of its passage]"
                if not got[1]:          # what stands above it, by the half that brought each
                    above = [("both" if c in dense and c in keyword else "vectors" if c in dense else "keywords" if c in keyword
                              else "spelling") for c in order[:got[0] - 1]]
                    mark += ("\n" + " " * 37 + f"from {ragtest.named(a.hits[cid].source_uri)}; above it: "
                             + ", ".join(f"{above.count(h)} {h}" for h in ("both", "vectors", "keywords", "spelling") if h in above))
                rows.append((got[0], f"  {got[0]:>5} {show(dense.get(cid)):>8} {show(keyword.get(cid)):>9}   {'yes' if got[1] else 'no':<8}{query}{mark}"))
            else:
                d, w = place_in(a.own, a.deep), place_in(a.own, a.words)
                rows.append((10**6, f"  {'-':>5} {'(' + show(d) + ')':>8} {'(' + show(w) + ')':>9}   {'no':<8}{query}"))
        print(f"\n   {ragtest.KINDS[kind]}\n  {'place':>5} {'vectors':>8} {'keywords':>9}   {'message':<8}")
        print("\n".join(text for _, text in sorted(rows, key=lambda r: r[0])))

    # ---- in which part of the order they stand
    print("\n2. Places, counted. A message has room for the first three or so.")
    head = "".join(f"{(str(a) if a == b else f'{a}-{b}'):>8}" for a, b in BANDS)
    print(f"  {'':<22}{head}{'not found':>11}")
    for kind in kinds:
        places = [now[id(a)][4][0] if now[id(a)][4] else None for a in asked if a.kind == kind]
        cells = "".join(f"{sum(1 for p in places if p and lo <= p <= hi):>8}" for lo, hi in BANDS)
        print(f"  {ragtest.KINDS[kind]:<22}{cells}{sum(1 for p in places if p is None):>11}")
    held = [retrieve.pick([a.hits[cid] for cid in now[id(a)][0][:TOP]], ROOM) for a in asked if a.kind in WORDS]
    if held:
        full = sum(1 for h in held if len(h) == TOP)
        print(f"  A message for one of the questions held {sum(len(h) for h in held) / len(held):.1f} passages on average "
              f"({min(len(h) for h in held)} to {max(len(h) for h in held)}; {TOP} is the most it takes, and {full} of {len(held)} had all {TOP}).")
        copies = [len(now[id(a)][0][:TOP]) - len(a.without_copies(now[id(a)][0][:TOP])) for a in asked if a.kind in WORDS]
        print(f"  Of the first {TOP} passages the search gives for a question, {sum(copies) / len(copies):.1f} on average are near "
              f"copies of one above them ({sum(1 for c in copies if c)} of {len(copies)} questions have at least one such copy).")

    # ---- what a reranker could choose from
    print("\n3. Is a passage that answers it there at all? (What no order can bring up: it was not found.)")
    print(f"  {'':<22}{'asked':>7}{f'among the {POOL} kept':>20}{'in all the halves bring':>26}{'100 from each':>15}{f'{DEEP} from each':>15}")
    for kind in kinds:
        mine = [a for a in asked if a.kind == kind]
        kept = sum(1 for a in mine if now[id(a)][4])
        counts = []
        for depth in (C, 100, DEEP):
            n = 0
            for a in mine:
                dense, keyword = a.lists(depth)
                pool = {*dense, *keyword, *(cid for group in a.pins for cid in group)}
                n += int(any(cid in a.found and a.answers(cid) for cid in pool))
            counts.append(n)
        print(f"  {ragtest.KINDS[kind]:<22}{len(mine):>7}{kept:>20}{counts[0]:>26}{counts[1]:>15}{counts[2]:>15}")

    # ---- the same test with another order
    print(f"\n4. What the same test would count with another order of what the two halves bring. first: stood first."
          f"\n   msg: in the message. {POOL}: among the {POOL} kept. (k: the constant of the fusion; smaller lets a passage that one"
          f"\n   half ranks high stand before passages both halves rank low.)")
    wide = max(len(v[0]) for v in VARIANTS) + 2
    print(f"  {'':<{wide}}" + "".join(f"{ragtest.KINDS[k] + ' (' + str(sum(1 for a in asked if a.kind == k)) + ')':>28}" for k in kinds))
    print(f"  {'':<{wide}}" + "".join(f"{'first':>14}{'msg':>7}{POOL:>7}" for _ in kinds))
    table = {}
    for label, depth, fuse, halves, room in VARIANTS:
        cells = ""
        for kind in kinds:
            first = sent = pool = 0
            for a in (a for a in asked if a.kind == kind):
                dense, keyword = a.lists(depth, halves)
                got = a.judge(ordered(dense, keyword, a.pins, a.found, fuse, POOL)[0], room)
                if got:
                    first, sent, pool = first + int(got[0] == 1), sent + int(got[1]), pool + 1
            table[(label, kind)] = (first, sent, pool)
            cells += f"{first:>14}{sent:>7}{pool:>7}"
        print(f"  {label:<{wide}}{cells}")
    differ = sum(1 for a in asked if a.near != a.deep[:C])
    if differ:
        print(f"  (For {differ} of {len(asked)} the nearest {C} are not the first {C} of the nearest {DEEP}: the vector index is "
              f"approximate. The rows with {C} from each half use the {C} the search itself gets.)")

    # ---- for the reranker try-out: what the halves bring for each question, kept in a file of its own
    out = {"collection": col.name, "top": TOP, "room": ROOM, "pool": POOL, "items": []}
    for a in (a for a in asked if a.kind in WORDS):
        order, spelled, dense, keyword, got = now[id(a)]
        union = [cid for cid in dict.fromkeys([*order, *dense, *keyword, *spelled]) if cid in a.found]
        out["items"].append({
            "kind": a.kind, "query": a.item["query"], "now": order, "pins": [cid for cid in spelled if cid in a.found],
            "good": [cid for cid in union if a.answers(cid)],
            "candidates": {cid: {"text": a.hits[cid].text, "source": ragtest.named(a.hits[cid].source_uri),
                                 "cost": cost(a.hits[cid]), "vectors": dense.get(cid), "keywords": keyword.get(cid)}
                           for cid in union}})
    scratch = path.with_name(f"{col.id}.candidates.json")
    scratch.write_text(json.dumps(out))
    scratch.chmod(0o600)
    print(f"\nKept for the reranker try-out: {scratch} ({scratch.stat().st_size / 1e6:.1f} MB: the passages the two halves "
          f"bring for each question). It is no part of the collection: delete it whenever you like.")
    return table


if __name__ == "__main__":
    sys.exit(0 if isinstance(asyncio.run(main()), dict) else 1)
