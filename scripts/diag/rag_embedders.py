"""What the kept search test of a collection would count with another embedding model.

A collection has one embedding model. Changing it means embedding every passage again: hours, in which the search
finds less, and no way back but the same hours. This asks first what there is to gain, with the embedding models
this machine's Ollama already holds. It changes nothing: not the collection, not its vectors, not the kept test.

Embedding the whole collection with every model is what it saves, so every model gets the same few thousand passages:

  for each question of the test   the 40 passages the keyword half brings for it, and the passages around the right
                                  one in its own document: what is most easily taken for it;
  from all over the collection    SAMPLE passages, every k-th in the order they were indexed;
  and for the second count        the 40 passages the vector half of today brings for each question.

They are counted two ways, because no one count is fair and sharp at once:

  like for like     every model over the passages no model chose (the first two groups). That is a small part of the
                    collection, so every model looks better than it is, today's model too. Who is ahead is what counts.
  against today     the same, and the 40 of today's model. Today's model then stands where it stands in the whole
                    collection: the first row of each table has that from the real index, and a check row repeats it.
                    For another model it is the best it could do: what it would find nearer in the rest of the
                    collection is not there.

One of the SAMPLE passages stands for as many passages of the collection as the sample is thin: about two hundred,
for 3,000 out of 600,000. Where one of them is nearer to a question than the right passage, that many places are
counted before the right one, which then is not among the 40: in the whole collection that many would stand there.

A model is asked as HunterAI asks today (the bare question, the bare passage) and, where its makers say it wants an
instruction in front of a question or of a passage, with that as well. HunterAI sends none today.
The identifiers of the test are left out: they are found by their spelling, whatever the model.

From the backend folder:   uv run python - < rag_embedders.py
WHATIF_COLLECTION: another collection than "RAG". EMBEDDERS: the models to try, as in "qwen3-embedding:8b,bge-large"
(left out: every embedding model this Ollama holds). SAMPLE: the passages from all over the collection (2000).

A diagnostic for one decision, not a part of HunterAI. Checked on two collections made of this project's own README
and source. One of 1,699 passages with a made-up embedding model: the first row of each table has the counts of
ragtest.run() and the check row repeats it; with every passage of the collection among the sample the like-for-like
rows are exact, and with a sample of 20 and of 200 the counts of a second model (all-minilm) came within one of its
counts over the whole collection. One of 319 passages embedded with bge-m3 in Ollama, beside qwen3-embedding:0.6b,
bge-large and all-minilm: the same checks held, and the two models that read 512 tokens refused the longer passages.
An instruction in front of the passages too (the second embedding of all of them) was run there with
jina-embeddings-v5-text-small-retrieval."""
import asyncio
import json
import os
import sys
import time

import httpx
import numpy as np

from hunterai import config, db
from hunterai.kb import embed, ragtest, search
from hunterai.kb.embed import EmbedError
from hunterai.kb.vectors import VectorError, get_store
from hunterai.providers import ollama
from hunterai.repo import kb

NAME = os.environ.get("WHATIF_COLLECTION", "RAG")
WANTED = [m.strip().removeprefix("ollama:") for m in os.environ.get("EMBEDDERS", "").split(",") if m.strip()]
SAMPLE = max(0, int(os.environ.get("SAMPLE", "2000")))
AROUND = 20                         # passages before the right one in its document, and as many behind it
C = search.CANDIDATES               # passages each half of the search brings
WORDS = ragtest.IN_WORDS            # the kinds that are asked in a person's words
PLACES = (1, 3, 8, C)
MOVED_SHOWN = 6                     # single questions that are shown for a model
come_from = getattr(ragtest, "named", lambda uri: uri)
SAY_EVERY = 30.0                    # seconds between two lines about where the embedding stands

UNSEEN = "unseen-"                  # in a list: a passage of the collection that is not here, which one of the sample stands for
# Families whose makers trained the vectors so that their front part can stand for the whole: such a vector is also
# counted cut to the size the collection's vectors have today, which is what would be kept on disk for the same room.
CUT = ("qwen3-embedding", "jina-embeddings-v5", "embeddinggemma", "snowflake-arctic-embed2", "nomic-embed-text", "mxbai-embed")
# What a family of models wants in front of a question and in front of a passage, by its makers' model card.
# The first name that is part of a model's name counts. A model of no family here is asked with the bare text only.
OWN = (
    ("qwen3-embedding", "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:", ""),
    ("jina-embeddings-v5", "Query: ", "Document: "),
    ("embeddinggemma", "task: search result | query: ", "title: none | text: "),
    ("snowflake-arctic-embed2", "query: ", ""),
    ("snowflake-arctic-embed", "Represent this sentence for searching relevant passages: ", ""),
    ("mxbai-embed", "Represent this sentence for searching relevant passages: ", ""),
    ("bge-large", "Represent this sentence for searching relevant passages: ", ""),
    ("bge-base", "Represent this sentence for searching relevant passages: ", ""),
    ("bge-small", "Represent this sentence for searching relevant passages: ", ""),
    ("nomic-embed-text", "search_query: ", "search_document: "),
)


def say(text: str) -> None:
    print(f"  {text}", file=sys.stderr, flush=True)


def short(text: str, limit: int = 96) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def by_name(name: str) -> bool:
    low = name.lower()
    return "embed" in low or low.startswith("bge-")


def plain(spec: str) -> str:
    """A model as it is named in the tables: "ollama:bge-large:latest" is "bge-large", and of a name with an owner
    or a host in front ("hf.co/jinaai/some-model-GGUF:Q8_0") the last part stands."""
    return spec.removeprefix("ollama:").removesuffix(":latest").rsplit("/", 1)[-1]


def tagged(name: str) -> str:
    """A model's name as Ollama lists it: "bge-m3" is "bge-m3:latest"."""
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def own_instruction(name: str) -> tuple[str, str] | None:
    low = name.lower()
    return next(((ask, passage) for family, ask, passage in OWN if family in low), None)


def lasting(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 90 else f"{minutes // 60} h {minutes % 60:02d} min"


async def held() -> dict[str, dict]:
    """The models this machine's Ollama holds: name -> whether it makes embeddings, the tokens it reads, the numbers
    in one of its vectors, its bytes. An older Ollama does not say what a model can do: its name then decides, as it
    does for the list of chat models in the app."""
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=3)) as client:
        tags = (await client.get(f"{ollama.host()}/api/tags")).json().get("models") or []
        for tag in tags:
            name = tag.get("name") or tag.get("model") or ""
            if not name:
                continue
            try:
                shown = (await client.post(f"{ollama.host()}/api/show", json={"model": name})).json()
            except (httpx.HTTPError, ValueError):
                shown = {}
            can = tag.get("capabilities") or shown.get("capabilities")
            info, details = shown.get("model_info") or {}, tag.get("details") or {}
            out[name] = {
                "embeds": "embedding" in can if can else by_name(name),
                "reads": next((v for k, v in info.items() if k.endswith(".context_length")), details.get("context_length")),
                "numbers": next((v for k, v in info.items() if k.endswith(".embedding_length")), details.get("embedding_length")),
                "bytes": tag.get("size") or 0}
    return out


async def read_all(embedder, texts: list[str], label: str = "") -> tuple[np.ndarray, list[int], float, str]:
    """Every text embedded, as the app embeds: EMBED_BATCH at a time, and one by one where a batch is refused.
    (a row of length 1 for each text, the texts the model refused, the seconds it took, what it said to those).
    A refused text has a row of zeros, and a text the model gave no usable vector for counts as refused."""
    rows: list = [None] * len(texts)
    refused: list[int] = []
    why = ""
    began = said = time.monotonic()
    for start in range(0, len(texts), embed.EMBED_BATCH):
        batch = texts[start:start + embed.EMBED_BATCH]
        try:
            got = await embedder.embed(batch)
        except EmbedError as e:
            if e.kind != "bad_request":
                raise
            got = []
            for text in batch:
                try:
                    got.append((await embedder.embed([text]))[0])
                except EmbedError as one:
                    if one.kind != "bad_request":
                        raise
                    got.append(None)
                    why = one.message
        for at, vector in enumerate(got, start):
            row = None if vector is None else np.asarray(vector, dtype=np.float32)
            if row is not None and not (np.isfinite(row).all() and row.any()):
                row, why = None, why or "it gave a vector that is no direction: all zeros, or not numbers"
            if row is None:
                refused.append(at)
            else:
                rows[at] = row
        now = time.monotonic()
        if label and now - said >= SAY_EVERY:
            said, done = now, start + len(batch)
            rate = done / max(now - began, 1e-6)
            say(f"{label}: {done:,} of {len(texts):,} passages ({rate:.1f} a second, about "
                f"{lasting((len(texts) - done) / rate)} left)")
    seconds = time.monotonic() - began
    size = next((len(r) for r in rows if r is not None), 1)
    matrix = np.zeros((len(texts), size), dtype=np.float32)
    for at, row in enumerate(rows):
        if row is not None:
            matrix[at] = row
    return unit(matrix), refused, seconds, why


def unit(matrix: np.ndarray) -> np.ndarray:
    """Every row at length 1 (a row of zeros stays one), so that a product of two rows is their cosine."""
    lengths = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(lengths == 0, 1.0, lengths)


class Asked:
    """One question of the test: its own passage, and what the two halves of the search bring for it today."""

    def __init__(self, item: dict, query: str, right: str, real: list, words: list, around: list):
        self.item, self.kind, self.query, self.own, self.right = item, item["kind"], query, item["chunk_id"], right
        self.real, self.words, self.around = real, words, around
        self._twins: dict[str, bool] = {}

    def answers(self, cid: str, texts: dict) -> bool:
        """Whether this passage answers it: its own passage, or a twin of that one, as the test counts."""
        if cid == self.own:
            return True
        if cid not in self._twins:
            self._twins[cid] = cid in texts and ragtest.shared(self.right, texts[cid]) >= ragtest.TWIN
        return self._twins[cid]

    def place(self, listed: list, texts: dict) -> int | None:
        """The place of the first passage of a list that answers it."""
        return next((at for at, (cid, _) in enumerate(listed, 1) if self.answers(cid, texts)), None)


class Stand:
    """Inside the search, in place of the embedding model and of the vector store. The search asks both as it always
    does and gets, for the question it asks, the list of the model that is being counted. Everything else is the
    search's own: its keyword half, the exact spellings, how the two halves are fused, how a message is filled."""

    def __init__(self, queries: list[str]):
        self.numbers = {query: at for at, query in enumerate(queries)}
        self.lists: list = []

    def get(self, spec, lock_timeout=None, **_):
        return self

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(self.numbers[texts[0]])]]

    async def release(self) -> None:
        return None

    async def search(self, name: str, vector: list[float], limit: int) -> list[tuple[str, float]]:
        return list(self.lists[int(vector[0])][:limit])


class Kept:
    """The app's own database calls, with what the keyword index answered kept: every row asks it the same things,
    and on a large collection each answer takes a moment."""

    def __init__(self, real):
        self._real, self._kept = real, {}

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def keyword_ids(self, conn, collection_ids, match, limit):
        key = ("words", tuple(collection_ids), match, limit)
        if key not in self._kept:
            self._kept[key] = await self._real.keyword_ids(conn, collection_ids, match, limit)
        return list(self._kept[key])

    async def exact_ids(self, conn, collection_ids, phrase, spelling, limit, *, any_case=False):
        key = ("exact", tuple(collection_ids), phrase, spelling, limit, any_case)
        if key not in self._kept:
            self._kept[key] = await self._real.exact_ids(conn, collection_ids, phrase, spelling, limit, any_case=any_case)
        return list(self._kept[key])


def listed(scores: np.ndarray, allowed: np.ndarray, lone: np.ndarray, ids: list[str], each: int) -> list[tuple[str, float]]:
    """What a model's vector half would bring for a question: the C passages it has nearest among the allowed ones.
    A passage that is here only as one of the sample takes `each` places: its own, and those of the passages of the
    collection it stands for, which are not here and are listed as unseen."""
    scores = np.where(allowed, scores, -np.inf)
    out: list[tuple[str, float]] = []
    for at in np.argsort(-scores, kind="stable"):
        if len(out) >= C or not allowed[at]:
            break
        out.append((ids[at], float(scores[at])))
        if lone[at]:
            out += [(f"{UNSEEN}{len(out) + n}", float(scores[at])) for n in range(each - 1)]
    return out[:C]


def counted(places: list, kinds: list[str], kind: str) -> str:
    mine = [p for p, k in zip(places, kinds) if k == kind]
    return "".join(f"{sum(1 for p in mine if p is not None and p <= most):>8}" for most in PLACES)


async def main() -> int:
    conn = await db.connect()
    loaded: list = []
    real_embed, real_store, real_kb = search.embed, search.get_store, search.kb
    try:
        col = next((c for c in await kb.list_collections(conn) if c.name == NAME), None)
        if col is None:
            print(f'There is no collection named "{NAME}".')
            return 1
        path = config.home() / "rag-tests" / f"{col.id}.json"
        if not path.exists():
            print(f'"{NAME}" has no kept test yet. Build one first: scripts/rag-test.sh "{NAME}"')
            return 1
        kept = json.loads(path.read_text())
        test = kept["test"]
        try:
            models = await held()
        except (httpx.HTTPError, ValueError) as e:
            print(f"Ollama does not answer at {ollama.host()} ({type(e).__name__}): there are no models to compare.")
            return 1
        today = col.embedder.removeprefix("ollama:")
        if WANTED:
            missing = [m for m in WANTED if tagged(m) not in models and m not in models]
            for m in missing:
                print(f"Ollama does not have {m}. To try it: ollama pull {m}")
            others = [m if m in models else tagged(m) for m in WANTED if m not in missing]
        else:
            others = [name for name, info in sorted(models.items()) if info["embeds"]]
        others = [m for m in dict.fromkeys(others) if tagged(m) != tagged(today)]
        if not others:
            print(f"This Ollama holds no embedding model other than the collection's own ({col.embedder}). Pull one to "
                  "compare, for example: ollama pull qwen3-embedding:0.6b")
            return 1
        total = (await (await conn.execute("select count(*) from kb_chunks where collection_id=?", (col.id,))).fetchone())[0]

        # ---- what the search brings today, for every question
        search.kb = words_kept = Kept(real_kb)
        store, vname = get_store(), kb.vector_name(col.id)
        mine = embed.get(col.embedder, None, stays_loaded=True)
        loaded.append(mine)
        items = [i for i in test["items"] if i["kind"] in WORDS]
        left_out = len(test["items"]) - len(items)
        asked: list[Asked] = []
        gone = 0
        for n, item in enumerate(items, 1):
            if n % 10 == 0 or n == len(items):
                say(f"asking {n} of {len(items)} as the search asks today")
            found = await kb.chunks_by_id(conn, [item["chunk_id"]])
            if item["chunk_id"] not in found:
                gone += 1                       # the collection no longer holds its passage: left out, as in the test
                continue
            chunk, doc = found[item["chunk_id"]]
            query = (item["query"] or "").strip()
            real = await store.search(vname, (await mine.embed([query]))[0], C)
            match = search.to_match_any(query)
            words = await words_kept.keyword_ids(conn, [col.id], match, C) if match else []
            around = [kb.chunk_id(doc.id, o) for o in range(max(0, chunk.ordinal - AROUND), chunk.ordinal + AROUND + 1)
                      if o != chunk.ordinal]
            asked.append(Asked(item, query, chunk.text, real, words, around))
        if not asked:
            print("The kept test has no question whose passage the collection still holds.")
            return 1
        kinds = [a.kind for a in asked]
        shown_kinds = [k for k in ragtest.KINDS if k in kinds]

        # ---- the passages every model is asked about
        sample = await kb.spread_chunks(conn, col.id, SAMPLE) if SAMPLE else []
        groups = {"right": {a.own for a in asked}, "words": {cid for a in asked for cid in a.words},
                  "around": {cid for a in asked for cid in a.around}, "today": {cid for a in asked for cid, _ in a.real},
                  "sample": {cid for cid, _, _ in sample}}
        texts = {cid: text for cid, text, _ in sample}
        aimed = sorted({*groups["right"], *groups["words"], *groups["around"], *groups["today"]} - set(texts))
        texts.update({cid: chunk.text for cid, (chunk, _) in (await kb.chunks_by_id(conn, aimed)).items()})
        ids = sorted(texts)
        at_of = {cid: at for at, cid in enumerate(ids)}
        mask = {name: np.array([cid in members for cid in ids]) for name, members in groups.items()}
        fair = mask["right"] | mask["words"] | mask["around"] | mask["sample"]     # passages no model chose
        whole = np.ones(len(ids), dtype=bool)
        lone = {"fair": mask["sample"] & ~(mask["right"] | mask["words"] | mask["around"]),
                "whole": mask["sample"] & ~(mask["right"] | mask["words"] | mask["around"] | mask["today"])}
        pools = {"fair": fair, "whole": whole}
        # One of the sample stands for the passages of the collection that are here by no other way, shared out evenly.
        each = {pool: max(1, round((total - int((pools[pool] & ~lone[pool]).sum())) / max(1, int(lone[pool].sum()))))
                for pool in pools}
        passages = [texts[cid] for cid in ids]

        print(f'Collection "{col.name}": {total:,} passages, embedding model {col.embedder}. {len(asked)} questions are '
              f'asked, written by {test.get("written_by")}'
              + (f"; the {left_out} identifiers of the test are left out" if left_out else "")
              + (f"; {gone} about passages the collection no longer holds too" if gone else "")
              + ".\nNothing in the collection or in the kept test is changed by this.")
        print(f"\nThe passages every model is asked about: {len(ids):,}. For the questions: the {int(mask['words'].sum()):,} "
              f"the keyword half brings, {int(mask['around'].sum()):,} around the right ones\nin their documents, and the "
              f"{int(mask['today'].sum()):,} the vector half of today brings. From all over the collection: "
              + (f"{int(mask['sample'].sum()):,}, each standing for about {each['whole']:,}." if mask["sample"].any()
                 else "none (SAMPLE=0), so nothing here stands for the rest of it."))
        if embed.on_cpu():
            print('"Run the embedding model on the processor" is on in Settings: every model here runs there too, which '
                  "takes hours for a small model\nand days for a large one, and the times below are a processor's. For this "
                  "run alone on the graphics card, stop it and start it again\nwith HUNTERAI_EMBED_ON_CPU=0 in front of the "
                  "command; the setting stays as it is.", flush=True)

        # ---- every model reads the same passages
        queries = [a.query for a in asked]
        rows: list[dict] = []           # label, model, pool, lists (one for each question)
        costs: list[dict] = []
        checked = None

        async def measure(name: str, embedder, is_today: bool) -> None:
            nonlocal checked
            spec = f"ollama:{name}" if not is_today else col.embedder
            info = models.get(tagged(name)) or models.get(name) or {}
            await embedder.release()            # as after every single question in the app: the model is not loaded
            began = time.monotonic()
            await embedder.embed(["a first request, which has to load the model"])
            loads = time.monotonic() - began
            matrix, refused, seconds, why = await read_all(embedder, passages, plain(spec))
            if len(refused) == len(passages):
                raise EmbedError("bad_request", why or "it embedded none of the passages")
            ok = np.ones(len(ids), dtype=bool)
            ok[refused] = False
            numbers = matrix.shape[1]
            cost = {"spec": spec, "numbers": numbers, "rate": (len(ids) - len(refused)) / max(seconds, 1e-6), "loads": loads,
                    "refused": len(refused), "why": why, "bytes": info.get("bytes") or 0, "reads": info.get("reads"),
                    "refused_sample": int((~ok & mask["sample"]).sum()), "own_refused": sum(1 for a in asked if not ok[at_of[a.own]])}
            costs.append(cost)
            ways = [("as HunterAI asks today", "", "", None)]
            wants = None if is_today else own_instruction(name)
            if wants:
                ways.append(("with its own instruction", wants[0], wants[1], None))
            if numbers > col.dimension and not is_today and any(family in name.lower() for family in CUT):
                ways.append((f"{'own instruction, ' if wants else ''}vectors cut to {col.dimension:,} numbers",
                             wants[0] if wants else "", wants[1] if wants else "", col.dimension))
            for way, ask, lead, cut in ways:
                docs = matrix
                if lead:
                    docs, again, _, _ = await read_all(embedder, [lead + t for t in passages], f"{plain(spec)}, {way}")
                    ok_here = ok.copy()
                    ok_here[again] = False
                else:
                    ok_here = ok
                asks, unasked, _, _ = await read_all(embedder, [ask + q for q in queries])
                if cut:
                    docs, asks = unit(docs[:, :cut]), unit(asks[:, :cut])
                for pool in ("fair", "whole"):
                    lists = []
                    for n, a in enumerate(asked):
                        if n in unasked:
                            lists.append([])
                            continue
                        lists.append(listed(docs @ asks[n], pools[pool] & ok_here, lone[pool], ids, each[pool]))
                    rows.append({"label": way, "spec": spec, "today": is_today, "pool": pool, "lists": lists})
            if is_today:                # a check of this way of counting: the real index against the same model here
                again = next(r for r in rows if r["today"] and r["pool"] == "whole")
                checked = sum(1 for a, got in zip(asked, again["lists"])
                              if (a.place(got, texts) or C + 1) == (a.place(a.real, texts) or C + 1))
            first = next(r for r in rows if r["spec"] == spec and r["pool"] == "whole")
            say(f"{plain(spec)} is done: in the count against today, the right passage is its nearest for "
                f"{sum(1 for a, got in zip(asked, first['lists']) if a.place(got, texts) == 1)} of {len(asked)} questions")

        await measure(today, mine, True)        # a failure here ends it: there is nothing to compare with
        await mine.release()
        loaded.remove(mine)
        failed: list[str] = []
        for name in others:
            say(f"loading {plain(name)}")
            other = embed.get(f"ollama:{name}", None, stays_loaded=True)
            loaded.append(other)
            try:
                await measure(name, other, False)
            except EmbedError as e:
                failed.append(f"{plain(name)} could not be asked ({e.message})")
            finally:
                await other.release()
                loaded.remove(other)
        for text in failed:
            print(text)
        real_row = {"label": "", "spec": col.embedder, "today": True, "pool": "real", "lists": [a.real for a in asked]}

        def order(pool: str) -> list[dict]:
            mine_first = sorted((r for r in rows if r["pool"] == pool), key=lambda r: not r["today"])
            return mine_first

        def named(row: dict) -> str:
            if row["pool"] == "real":
                return f"{plain(row['spec'])} today: the real index, whole collection"
            if row["today"]:
                return f"{plain(row['spec'])}, embedded again here" + (" (a check: as the first row)" if row["pool"] == "whole" else "")
            return f"{plain(row['spec'])}, {row['label']}"

        parts = (("fair", "like for like: over the passages no model chose (a small pool: every model looks better than it is)"),
                 ("whole", f"against today: the same, and the {C} of today's model (for another model: the best it could do)"))
        wide = max(len(named(r)) for r in [real_row, *rows]) + 2

        # ---- 1. the vector half on its own
        print(f"\n1. The vector half on its own. In how many cases a passage that answers stands first, among the first 3, "
              f"the first 8, the {C} it brings.")
        print(f"  {'':<{wide}}" + "".join(f"{ragtest.KINDS[k] + ' (' + str(kinds.count(k)) + ')':>32}" for k in shown_kinds))
        print(f"  {'':<{wide}}" + "".join(f"{'first':>8}{'in 3':>8}{'in 8':>8}{f'in {C}':>8}" for _ in shown_kinds))

        def vector_row(row: dict) -> None:
            places = [a.place(got, texts) for a, got in zip(asked, row["lists"])]
            row["places"] = places
            print(f"  {named(row):<{wide}}" + "".join(counted(places, kinds, k) for k in shown_kinds))

        vector_row(real_row)
        for pool, title in parts:
            print(f"  -- {title}")
            for row in order(pool):
                vector_row(row)
        if checked is not None:
            print(f"  (The check: embedded again here, {col.embedder} has the right passage at the place the real index "
                  f"has it for {checked} of {len(asked)} questions.)")

        # ---- 2. the whole test with that vector half
        print(f"\n2. The whole test with that vector half: the keyword half, the exact spellings, the fusing and the filling "
              f"of a message stay\n   the search's own. first: stood first. msg: in the message. {C}: among the {C} the "
              "search keeps.")
        print(f"  {'':<{wide}}" + "".join(f"{ragtest.KINDS[k] + ' (' + str(kinds.count(k)) + ')':>32}" for k in shown_kinds))
        print(f"  {'':<{wide}}" + "".join(f"{'first':>16}{'msg':>8}{C:>8}" for _ in shown_kinds))
        stand = Stand(queries)
        search.embed, search.get_store = stand, lambda: stand
        ask_these = {**test, "items": [a.item for a in asked]}

        async def test_row(row: dict) -> None:
            stand.lists = row["lists"]
            got = (await ragtest.run(conn, ask_these))["kinds"]
            row["test"] = got
            cells = "".join(f"{got.get(k, {}).get('first', 0):>16}{got.get(k, {}).get('sent', 0):>8}"
                            f"{got.get(k, {}).get('pool', 0):>8}" for k in shown_kinds)
            print(f"  {named(row):<{wide}}{cells}")

        say("counting the whole test for every row")
        await test_row(real_row)
        for pool, title in parts:
            print(f"  -- {title}")
            for row in order(pool):
                await test_row(row)
        search.embed, search.get_store = real_embed, real_store
        last = (kept.get("runs") or [{}])[-1].get("kinds") or {}
        if last:
            print("  (The last run of scripts/rag-test.sh counted: " + "; ".join(
                f"{ragtest.KINDS[k]} {last[k]['first']} / {last[k]['sent']} / {last[k]['pool']}" for k in shown_kinds if k in last)
                + ". The first row should say the same, unless the search was changed since.)")

        # ---- 3. what a change would cost
        print(f"\n3. What it would cost on this machine. Measured on the {len(ids):,} passages above, {embed.EMBED_BATCH} at a "
              f"time as the app embeds. \"all of it\": the {total:,} passages\n   of the collection embedded again, and the "
              "room their vectors take.")
        cost_wide = max(len(plain(c["spec"])) for c in costs) + 2
        print(f"  {'':<{cost_wide}}{'on disk':>10}{'reads':>15}{'a vector':>16}{'loads in':>11}{'a second':>10}{'all of it':>13}"
              f"{'vectors':>10}   passages it refused")
        for c in costs:
            size = f"{c['bytes'] / 1e9:.1f} GB" if c["bytes"] >= 1e9 else f"{c['bytes'] / 1e6:.0f} MB" if c["bytes"] else "?"
            reads = f"{c['reads']:,} tokens" if c["reads"] else "?"
            refused = "none"
            if c["refused"]:
                refused = f"{c['refused']:,} of {len(ids):,}"
                if mask["sample"].any():
                    share = c["refused_sample"] / int(mask["sample"].sum())
                    refused += (f"; of the sample {c['refused_sample']:,} ({share:.0%}), which is about "
                                f"{share * total:,.0f} of the collection")
                refused += f': "{short(c["why"], 60)}"'
            print(f"  {plain(c['spec']):<{cost_wide}}{size:>10}{reads:>15}{c['numbers']:>8,} numbers{c['loads']:>9.1f} s{c['rate']:>10.1f}"
                  f"{lasting(total / max(c['rate'], 1e-6)):>13}{total * c['numbers'] * 4 / 1e9:>7.1f} GB   {refused}")
        print("  \"loads in\": what the first request waited after the model was unloaded. HunterAI unloads the embedding "
              "model after every single\n  question (HUNTERAI_EMBED_KEEP_LOADED keeps it), so a search waits that long each "
              "time, and longer where the chat model\n  has to leave the graphics card for it.")
        if any(c["refused"] for c in costs):
            print("  A passage a model refuses is longer than the model reads. Embedding a collection again removes such "
                  "passages from it, text and all.")
        if any(c["own_refused"] for c in costs):
            print("  " + "; ".join(f"{plain(c['spec'])} refused the right passage of {c['own_refused']} "
                                   f"question{'' if c['own_refused'] == 1 else 's'}" for c in costs if c["own_refused"])
                  + ". For that model they count as not found.")

        # ---- 4. single questions
        print(f"\n4. Single questions, in the count against today: where the vector half has a passage that answers "
              f"(- : not among its {C}).\n   For each model the way of asking that puts most of them first. A place "
              "that got worse is sure; one that got better is the best it could be.")
        was = real_row["places"]
        for spec in dict.fromkeys(r["spec"] for r in rows if not r["today"]):
            ways = [r for r in rows if r["spec"] == spec and r["pool"] == "whole"]
            best = max(ways, key=lambda r: (sum(1 for p in r["places"] if p == 1), sum(1 for p in r["places"] if p and p <= 8)))
            moved = [(a, before, after) for a, before, after in zip(asked, was, best["places"])
                     if ((before or 99) <= 8) != ((after or 99) <= 8)]
            better = sum(1 for _, _, after in moved if after and after <= 8)
            print(f"\n   {plain(spec)}, {best['label']}: into the first 8 for {better}, out of them for {len(moved) - better}.")
            for a, before, after in sorted(moved, key=lambda m: (m[2] or 99))[:MOVED_SHOWN]:
                print(f"     {'+' if after and after <= 8 else '-'} {short(a.query, 100)}\n"
                      f"         place {before or '-'} -> {after or '-'}   ({come_from(a.item['source'])})")
            if len(moved) > MOVED_SHOWN:
                print(f"     ... and {len(moved) - MOVED_SHOWN} more")

        print(f"\nHow to read it. A model is worth the hours only where it is ahead in both counts: of {plain(col.embedder)} "
              "embedded again in the like-for-like\ncount, and of the first row in the count against today. Ahead in the "
              "second alone, it had its head start there and no more.\nA difference of one or two questions is chance at "
              "this size. \"with its own instruction\" needs a change in HunterAI before it is\nreal: today a question is "
              "embedded as it is written.")
        return 0
    except EmbedError as e:
        print(f"The collection's own embedding model could not be asked ({e.message}). Nothing was counted.")
        return 1
    except VectorError as e:
        print(f"The vector store does not answer ({e}). Start HunterAI, which starts it, and run this again.")
        return 1
    finally:
        search.embed, search.get_store, search.kb = real_embed, real_store, real_kb
        for embedder in loaded:
            await embedder.release()
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
