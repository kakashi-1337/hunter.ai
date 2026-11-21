"""What the kept search test of a collection would count if every passage knew where it stands.

Today the search sees the text of a passage and nothing else: not the file or the page it comes from, not the title of
that page, not the headings above it. This gives every passage a layout line, made by rules and with no model:

    <where it comes from> > <the title of its page> > <the headings above it>

and asks what the kept test would count with it. Nothing in the collection or in the kept test is changed.

- The keyword half is measured over the whole collection: a keyword index of its own is built in a temporary file
  (the layout line beside the text of every passage, once with the words as written and once with word stems), and
  the file is deleted at the end.
- The vector half cannot be measured without embedding every passage again. What is measured in its place: among the
  passages the search brings today for a question, and the right one, where the right one stands when all of them
  are embedded with their layout line. That is a hint, not a count.
- Two things that need no layout line are counted on the way: the first passage of each half standing first, and
  more room in the message.

From the backend folder:   uv run python - < rag_layout.py
WHATIF_COLLECTION: another collection than "RAG". LAYOUT_VECTORS=0: without the part about the vector half.

A diagnostic for one decision, not a part of HunterAI. Checked on three made-up collections: its row "as it is now"
has the counts of ragtest.run() for every kind, and the temporary index, asked for the text of the passages alone,
brings 5,386 of the 5,453 passages the app's own index brings for the 177 things those tests ask. Timed on a fourth
of 150,000 passages, on two slow cores: 16 s to build the index (212 MB), under a second for a question."""
import asyncio
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from hunterai import config, db
from hunterai.kb import embed, parsers, ragtest, retrieve, search
from hunterai.kb.vectors import get_store
from hunterai.repo import kb
from hunterai.types import Hit

NAME = os.environ.get("WHATIF_COLLECTION", "RAG")
VECTORS = os.environ.get("LAYOUT_VECTORS", "1") != "0"
C, TOP, ROOM, POOL, K = search.CANDIDATES, ragtest.TOP, ragtest.ROOM, ragtest.POOL, search.RRF_K
WORDS = ragtest.IN_WORDS            # the kinds that are asked in a person's words
DEEP = 200                          # passages a keyword index is asked for: enough to say where a passage stands
SECTION_MAX, TITLE_MAX, HEADING_MAX = 160, 150, 80
MIN_FREE = 3 * 1024**3              # bytes the data folder must have free for the temporary index
BATCH = 5000
SHOWN = 8                           # layout lines that are shown
MORE_ROOM = 9000

_PIECES = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_HEADING = re.compile(r"(#{1,6})\s+(.*\S)")


def inner_words(name: str) -> str:
    """The words inside a name that a keyword index does not see: for webSourceMap.test.ts "web source map", and
    for open-design also "opendesign"."""
    out: list[str] = []
    for part in re.split(r"[^A-Za-z0-9_-]+", name):
        if re.search(r"[A-Za-z0-9][-_]+[A-Za-z0-9]", part):
            out.append(re.sub(r"[-_]+", "", part).lower())
        for piece in re.split(r"[-_]+", part):
            pieces = _PIECES.findall(piece)
            if len(pieces) > 1:
                out += [p.lower() for p in pieces]
    return " ".join(dict.fromkeys(out))


class Doc:
    """A document while its passages go by in their order: what stood above the passage that comes next."""

    __slots__ = ("extra", "fenced", "form", "last", "name", "stack", "title")

    def __init__(self, uri: str):
        web = uri.startswith(("http://", "https://"))
        last = (urlsplit(uri).path if web else uri.split("!/")[-1]).rsplit("/", 1)[-1]
        form = parsers.kind_for(last)
        self.form = "html" if form == "html" or (web and form is None) else "markdown" if form == "markdown" else ""
        self.name = ragtest.named(uri)
        self.extra = inner_words(self.name)
        self.title, self.stack, self.fenced, self.last = "", [], False, -1

    def layout(self, ordinal: int, text: str) -> str:
        """The layout line of the next passage. A page's title is the first line of its first passage (the parser
        puts it there); the headings are those of a Markdown file, outside its code blocks."""
        if ordinal <= self.last:                # not in its order: what stood above it is not known
            self.stack, self.fenced = [], False
        self.last = ordinal
        section = ""
        if self.form == "markdown":
            # A passage that begins with a heading stands under the headings above that one, not beside its sisters.
            own = None if self.fenced else _HEADING.match(text)
            section = " > ".join(heading for level, heading in self.stack if own is None or level < len(own.group(1)))
            for line in text.split("\n"):
                if line.lstrip().startswith(("```", "~~~")):
                    self.fenced = not self.fenced
                elif not self.fenced:
                    found = _HEADING.match(line)
                    if found:
                        level = len(found.group(1))
                        while self.stack and self.stack[-1][0] >= level:
                            self.stack.pop()
                        self.stack.append((level, found.group(2).rstrip("#").strip()[:HEADING_MAX]))
        elif self.form == "html" and ordinal == 0:
            first = text.split("\n", 1)[0].strip()
            self.title = first if len(first) <= TITLE_MAX else ""
            return self.name                    # the title is its own first line
        if len(section) > SECTION_MAX:
            section = "... " + section[-SECTION_MAX:]
        return " > ".join(part for part in (self.name, self.title, section) if part)


def build(col, path) -> tuple[sqlite3.Connection, dict]:
    """The temporary index: every passage of the collection beside its layout line, with the words as written
    (`plain`) and as word stems (`stem`). Read from the app's database, which is opened for reading only."""
    src = sqlite3.connect(f"file:{config.db_path()}?mode=ro", uri=True)
    out = sqlite3.connect(path)
    out.executescript(
        "pragma journal_mode=off; pragma synchronous=off; "
        "create table ids(n integer primary key, id text not null, shown text not null); "
        "create virtual table plain using fts5(ctx, text, content='', tokenize='unicode61'); "
        "create virtual table stem using fts5(ctx, text, content='', tokenize='porter unicode61');")
    docs = {row[0]: Doc(row[1]) for row in src.execute(
        "select id, source_uri from kb_documents where collection_id=? and status='ok'", (col.id,))}
    total = src.execute("select count(*) from kb_chunks where collection_id=?", (col.id,)).fetchone()[0]
    ids: list[tuple] = []
    rows: list[tuple] = []
    done = unordered = 0
    began = time.monotonic()

    def flush() -> None:
        out.executemany("insert into ids values(?,?,?)", ids)
        out.executemany("insert into plain(rowid, ctx, text) values(?,?,?)", rows)
        out.executemany("insert into stem(rowid, ctx, text) values(?,?,?)", rows)
        ids.clear()
        rows.clear()

    for n, cid, did, ordinal, text in src.execute(
            "select n, id, document_id, ordinal, text from kb_chunks where collection_id=? order by n", (col.id,)):
        doc = docs.get(did)
        if doc is None:
            continue                            # of a document that is not ok: the search does not bring it either
        unordered += int(ordinal <= doc.last)
        shown = doc.layout(ordinal, text)
        ids.append((n, cid, shown))
        rows.append((n, f"{shown} {doc.extra}", text))
        done += 1
        if len(ids) >= BATCH:
            flush()
            if done % (10 * BATCH) == 0:
                print(f"  indexing {done:,} of {total:,} passages", file=sys.stderr, flush=True)
    flush()
    out.execute("create index ids_id on ids(id)")
    out.commit()
    src.close()
    return out, {"passages": done, "documents": len(docs), "unordered": unordered,
                 "seconds": time.monotonic() - began}


def keyword(out: sqlite3.Connection, table: str, match: str, layout_weight: float) -> list[str]:
    """Chunk ids from the temporary index, best first, as the app asks its own index. A weight of 0 for the layout
    line: the text of the passages alone is asked."""
    if not match:
        return []
    asked = match if layout_weight else "{text} : (" + match + ")"
    return [row[0] for row in out.execute(
        f"select i.id from {table} f join ids i on i.n = f.rowid where {table} match ? "
        f"order by bm25({table}, {float(layout_weight)}, 1.0), f.rowid limit ?", (asked, DEEP))]


def fused_order(dense: dict, words: dict, pins: list, found, lead: str = "", k: int = K) -> list[str]:
    """The order the search gives: search.search() from its two lists on. With `lead`, the first passage of each
    half is brought up, behind an exact spelling: "first", they stand before all others; "behind", they stand right
    behind the passage the fused order has first, which keeps its place."""
    fused = {cid: (1 / (k + dense[cid]) if cid in dense else 0) + (1 / (k + words[cid]) if cid in words else 0)
             for cid in {*dense, *words}}
    spelled = search.put_first(pins, fused)
    scored = sorted((-(fused.get(cid, 0) + (search.EXACT_SCORE if cid in spelled else 0)), dense.get(cid, 10**9), cid)
                    for cid in {*fused, *spelled})
    order = [cid for _, _, cid in scored if cid in found]
    if lead:
        rest = [cid for cid in order if cid not in spelled]
        head = rest[:1] if lead == "behind" else []
        firsts: list[str] = []
        for ranks in (dense, words):
            best = min((cid for cid in ranks if cid in found), key=ranks.get, default=None)
            if best is not None and best not in spelled and best not in head and best not in firsts:
                firsts.append(best)
        order = ([cid for cid in order if cid in spelled] + head + firsts
                 + [cid for cid in rest if cid not in head and cid not in firsts])
    return order[:POOL]


class Asked:
    """One thing the test asks, with what the vector half and every keyword index bring for it."""

    def __init__(self, item: dict, vector: list, dense: list, pins: list, lists: dict, found: dict):
        self.item, self.kind, self.own = item, item["kind"], item["chunk_id"]
        self.vector, self.pins, self.lists = vector, pins, lists
        self.dense = {cid: rank for rank, cid in enumerate(dense, 1)}
        self.hits = {cid: Hit(chunk_id=cid, document_id=doc.id, collection_id=doc.collection_id,
                              source_uri=doc.source_uri, ordinal=chunk.ordinal, text=chunk.text,
                              line_start=chunk.line_start, line_end=chunk.line_end, score=0.0)
                     for cid, (chunk, doc) in found.items()}
        self.right = found[self.own][0].text if self.own in found else None
        self._shares: dict = {}

    def answers(self, cid: str) -> bool:
        """Whether this passage answers it: its own passage, or for a question a twin of that one."""
        if cid == self.own:
            return True
        if self.kind not in WORDS or cid not in self.hits:
            return False
        if cid not in self._shares:
            self._shares[cid] = ragtest.shared(self.right, self.hits[cid].text)
        return self._shares[cid] >= ragtest.TWIN

    def ranks(self, name: str) -> dict:
        return {cid: rank for rank, cid in enumerate(self.lists[name][:C], 1)}

    def order(self, name: str, lead: str = "", k: int = K) -> list[str]:
        return fused_order(self.dense, self.ranks(name), self.pins, self.hits, lead, k)

    def judge(self, order: list[str], room: int = ROOM):
        """(the place of the best passage that answers it, whether one that answers it is in the message), or None:
        counted as ragtest.run() counts."""
        sent = {s.chunk_id for s in retrieve.pick([self.hits[cid] for cid in order[:TOP]], room)}
        if order and order[0] == self.own:
            return 1, self.own in sent
        good = [place for place, cid in enumerate(order, 1) if self.answers(cid)]
        return (min(good), any(order[place - 1] in sent for place in good)) if good else None

    def in_half(self, name: str):
        """The place of a passage that answers it in a keyword index's own list (its first DEEP), or None."""
        listed = self.lists[name]
        good = [place for place, cid in enumerate(listed[:C], 1) if self.answers(cid)]
        return min(good) if good else (listed.index(self.own) + 1 if self.own in listed else None)


SMALL_K = 5
ROWS = [    # what it is called, the keyword index, each half's first passage brought up, characters of room, the constant
    ("as it is now", "now", "", ROOM, K),
    ("now, the first passage of each half stands first", "now", "first", ROOM, K),
    ("now, they stand right behind the fused first", "now", "behind", ROOM, K),
    (f"now, with room for {MORE_ROOM:,} characters", "now", "", MORE_ROOM, K),
    ("the layout line in the keyword index", "layout", "", ROOM, K),
    ("the same, the layout line counted double", "layout x2", "", ROOM, K),
    ("the layout line, each half's first passage first", "layout", "first", ROOM, K),
    ("the layout line, they stand behind the fused first", "layout", "behind", ROOM, K),
    (f"the same, with room for {MORE_ROOM:,} characters", "layout", "behind", MORE_ROOM, K),
    (f"the layout line, the two halves fused with k={SMALL_K}", "layout", "", ROOM, SMALL_K),
    ("word stems in the keyword index, no layout line", "stems", "", ROOM, K),
    ("word stems and the layout line", "stems+layout", "", ROOM, K),
]
HALVES = [("now", "as it is now"), ("layout", "with the layout line"), ("stems", "with word stems"),
          ("stems+layout", "with both")]


def short(text: str, limit: int = 84) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / ((math.sqrt(sum(x * x for x in a)) or 1.0) * (math.sqrt(sum(y * y for y in b)) or 1.0))


async def embedded(embedder, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for start in range(0, len(texts), embed.EMBED_BATCH):
        out += await embedder.embed(texts[start:start + embed.EMBED_BATCH])
    return out


def said(got) -> str:
    return "not found" if got is None else f"place {got[0]}, {'in' if got[1] else 'not in'} the message"


async def main() -> int:
    conn = await db.connect()
    embedder = out = None
    tmp = None
    try:
        col = next((c for c in await kb.list_collections(conn) if c.name == NAME), None)
        if col is None:
            print(f'There is no collection named "{NAME}".')
            return 1
        path = config.home() / "rag-tests" / f"{col.id}.json"
        if not path.exists():
            print(f'"{NAME}" has no kept test yet. Build one first: scripts/rag-test.sh "{NAME}"')
            return 1
        if shutil.disk_usage(path.parent).free < MIN_FREE:
            print(f"The data folder has less than {MIN_FREE // 1024**3} GB free: the temporary index is not built.")
            return 1
        test = json.loads(path.read_text())["test"]
        items = test["items"]
        print(f'Collection "{col.name}", embedding model {col.embedder}. {len(items)} things are asked, the questions '
              f'written by {test.get("written_by")}. Nothing in the collection or in the kept test is changed by this.')
        tmp = path.with_name(f"{col.id}.layout.tmp.db")
        tmp.unlink(missing_ok=True)
        print("  building a keyword index of its own in a temporary file ...", file=sys.stderr, flush=True)
        out, built = build(col, tmp)
        print(f"A keyword index of its own for this: {built['passages']:,} passages of {built['documents']:,} documents, "
              f"each beside its layout line, in {built['seconds']:.0f} s ({tmp.stat().st_size / 1e6:,.0f} MB in a temporary "
              "file, which is deleted at the end)."
              + (f" {built['unordered']:,} passages did not come in the order of their document: the headings above "
                 "them are not known." if built["unordered"] else ""))

        store, embedder = get_store(), embed.get(col.embedder, None, stays_loaded=True)
        vname = kb.vector_name(col.id)
        asked: list[Asked] = []
        gone = 0
        for n, item in enumerate(items, 1):
            if n % 10 == 0 or n == len(items):
                print(f"  asking {n} of {len(items)}", file=sys.stderr, flush=True)
            query = (item["query"] or "").strip()
            vector = (await embedder.embed([query]))[0]
            dense = [cid for cid, _ in await store.search(vname, vector, C)]
            match = search.to_match_any(query)
            lists = {"now": await kb.keyword_ids(conn, [col.id], match, DEEP) if match else [],
                     "text": keyword(out, "plain", match, 0), "layout": keyword(out, "plain", match, 1),
                     "layout x2": keyword(out, "plain", match, 2), "stems": keyword(out, "stem", match, 0),
                     "stems+layout": keyword(out, "stem", match, 1)}
            pins = await search.exact(conn, [col.id], query)
            ids = {*dense, *(cid for listed in lists.values() for cid in listed[:C]),
                   *(cid for group in pins for cid in group), item["chunk_id"]}
            one = Asked(item, vector, dense, pins, lists, await kb.chunks_by_id(conn, list(ids)))
            if one.right is None:
                gone += 1                       # its passage is no longer in the collection: left out, as in the test
                continue
            asked.append(one)
        if gone:
            print(f"{gone} of them {'is about a passage' if gone == 1 else 'are about passages'} the collection no "
                  "longer holds: left out, as the test leaves them out.")
        kinds = [k for k in ragtest.KINDS if any(a.kind == k for a in asked)]
        mine = {k: [a for a in asked if a.kind == k] for k in kinds}
        now = {id(a): a.judge(a.order("now")) for a in asked}
        new = {id(a): a.judge(a.order("layout")) for a in asked}

        # ---- 1. what a layout line looks like
        low = sorted((a for a in asked if a.kind in WORDS and not (now[id(a)] and now[id(a)][1])),
                     key=lambda a: now[id(a)] is not None)[:SHOWN]
        if low:
            print(f"\n1. What a passage would carry. For {len(low)} questions whose passage is not in the message today: "
                  "the layout line of that\n   passage, and how its own text begins (which is all the search sees of it now).")
            for a in low:
                line = out.execute("select shown from ids where id=?", (a.own,)).fetchone()
                print(f"\n   {short(a.item['query'], 110)}\n     layout line: {short(line[0] if line else '?', 150)}"
                      f"\n     its text:    {short(a.right, 110)}")

        # ---- 2. the keyword half on its own
        print(f"\n2. The keyword half on its own, over the whole collection: in how many cases a passage that answers "
              f"stands among\n   the {C} it brings (and how often it is the first of them).")
        print(f"  {'':<22}{'asked':>7}" + "".join(f"{label:>24}" for _, label in HALVES))
        for k in kinds:
            cells = ""
            for name, _ in HALVES:
                places = [a.in_half(name) for a in mine[k]]
                among = sum(1 for p in places if p and p <= C)
                cells += f"{f'{among} ({sum(1 for p in places if p == 1)} first)':>24}"
            print(f"  {ragtest.KINDS[k]:<22}{len(mine[k]):>7}{cells}")
        common = [len({*a.lists["now"][:C]} & {*a.lists["text"][:C]}) for a in asked]
        full = [len(a.lists["now"][:C]) for a in asked]
        print(f"  (A check of the temporary index: asked for the text of the passages alone, it brings {sum(common):,} "
              f"of the {sum(full):,} passages the app's own index brings for these {len(asked)} things.)")

        # ---- 3. the test, counted again
        print(f"\n3. The test, counted again. first: stood first. msg: in the message. {POOL}: among the {POOL} the search "
              f"keeps.\n   The vector half is the one of today in every row. k: the constant of the fusion, {K} today.")
        wide = max(len(row[0]) for row in ROWS) + 2
        print(f"  {'':<{wide}}" + "".join(f"{ragtest.KINDS[k] + ' (' + str(len(mine[k])) + ')':>28}" for k in kinds))
        print(f"  {'':<{wide}}" + "".join(f"{'first':>14}{'msg':>7}{POOL:>7}" for _ in kinds))
        for label, name, lead, room, constant in ROWS:
            cells = ""
            for k in kinds:
                got = [a.judge(a.order(name, lead, constant), room) for a in mine[k]]
                cells += (f"{sum(1 for g in got if g and g[0] == 1):>14}{sum(1 for g in got if g and g[1]):>7}"
                          f"{sum(1 for g in got if g):>7}")
            print(f"  {label:<{wide}}{cells}")

        # ---- 4. what moved
        moved = [(a, now[id(a)], new[id(a)]) for a in asked
                 if (now[id(a)] is None) != (new[id(a)] is None)
                 or (now[id(a)] and new[id(a)] and now[id(a)][1] != new[id(a)][1])]
        print("\n4. What changes for single things with the layout line in the keyword index (\"as it is now\" against "
              "\"the layout line in the keyword index\"):" + ("" if moved else " nothing."))
        for a, was, got in sorted(moved, key=lambda m: (m[2] is None, m[2] is not None and not m[2][1])):
            better = got is not None and (was is None or (got[1] and not was[1]))
            print(f"   {'+' if better else '-'} {short(a.item['query'], 96)}\n       {said(was)} -> {said(got)}"
                  f" (in the keyword half: {a.in_half('now') or '-'} -> {a.in_half('layout') or '-'})")

        # ---- 5. the vector half, as far as it can be said without embedding the collection again
        if VECTORS:
            print("\n5. The vector half. For each question: the passages the search brings today (both halves, with and "
                  "without the layout\n   line in the keyword index) and the right one, embedded as they are and "
                  "embedded with their layout line above the text.\n   Where a passage that answers stands among them, "
                  "nearest first. A hint only: the rest of the collection is not embedded again.")
            rows = []
            words = [a for a in asked if a.kind in WORDS]
            for n, a in enumerate(words, 1):
                if n % 5 == 0 or n == len(words):
                    print(f"  embedding the passages of question {n} of {len(words)}", file=sys.stderr, flush=True)
                ids = [cid for cid in dict.fromkeys([*a.dense, *a.lists["now"][:C], *a.lists["layout"][:C], a.own])
                       if cid in a.hits]
                marks = ",".join("?" * len(ids))
                lines = dict(out.execute(f"select id, shown from ids where id in ({marks})", ids).fetchall())
                plain = await embedded(embedder, [a.hits[cid].text for cid in ids])
                laid = await embedded(embedder, [f"{lines.get(cid, '')}\n{a.hits[cid].text}" for cid in ids])
                places = []
                for vectors in (plain, laid):
                    near = sorted(range(len(ids)), key=lambda i, v=vectors, q=a.vector: -cosine(q, v[i]))
                    places.append(next(place for place, i in enumerate(near, 1) if a.answers(ids[i])))
                rows.append((a, len(ids), places[0], places[1]))
            print(f"  {'':<22}{'asked':>7}{'as they are: first / in 3 / in 8':>36}{'with the layout line: first / in 3 / in 8':>44}")
            for k in kinds:
                got = [r for r in rows if r[0].kind == k]
                if got:
                    cells = "".join(f"{' / '.join(str(sum(1 for r in got if r[at] <= top)) for top in (1, 3, 8)):>{w}}"
                                    for at, w in ((2, 36), (3, 44)))
                    print(f"  {ragtest.KINDS[k]:<22}{len(got):>7}{cells}")
            lost = [r for r in rows if now[id(r[0])] is None]
            if lost:
                print("   The questions that are not found today:")
                for a, among, was, got in lost:
                    print(f"     among {among:>3}: place {was:>3} -> {got:>3}   {short(a.item['query'], 90)}")
        return 0
    except embed.EmbedError as e:
        print(f"The embedding model could not be asked ({e.message}). Is Ollama running?")
        return 1
    finally:
        if embedder is not None:
            await embedder.release()
        if out is not None:
            out.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
