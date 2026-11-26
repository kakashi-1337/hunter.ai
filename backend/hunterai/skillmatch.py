"""Which skills a request names. With a few skills installed, all of them, as before. With many, the few whose
descriptions fit the message best: every skill named costs its line in every request, and a small local model picks
from a short list what it does not find in a long one.

Fit is judged by meaning when it costs nothing: the lookup in a chat's collections and the recall of notes have
embedded the message already (search.QueryVectors), and the descriptions are embedded once, by the worker, and again
when one changes. A message in Tagalog then finds a description in English. Naming skills never makes an embedding
request of its own while a run waits: without a vector of the message, or while descriptions are still to be
embedded, fit is judged by the words the two share."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .kb import embed
from .kb.embed import EmbedError
from .kb.search import QueryVectors
from .kb.vectors import _pack, _unpack
from .repo import jobs, settings
from .repo import skills as store

SHOWN = 8                   # skills a request names when more are installed
KIND = "skills"             # the worker's job that embeds the descriptions
TEXT_CHARS = 1200           # of a name and its description
QUERY_CHARS = 2000          # as the lookups cut the message (kb/retrieve.py, memory.py): the same words, the same vector
RETRY_AFTER = 600           # seconds before a job that failed is asked for again
LOCK_TIMEOUT = 120

_WORD = re.compile(r"[a-z0-9]{3,}")
_SMALL = frozenset("the and for with from that this what when where how are was were can could should would you your "
                   "use owner asks about ang mga para kung ano paano".split())


@dataclass(frozen=True)
class Fit:
    shown: list[tuple[store.Skill, float]]      # the skills to name, the best fit first, each with its score
    of: int                                     # how many could have been named
    by: str                                     # "all" (few enough to name them all) | "meaning" | "words"


async def spec(conn) -> str:
    """The embedder of HunterAI's own lookups: the one the memory uses, so that a message is embedded once for both."""
    return await settings.get(conn, "memory_embedder") or embed.DEFAULT_EMBEDDER


async def candidates(conn) -> list[store.Skill]:
    """The skills the model may load by itself, by name."""
    return [s for s in await store.list(conn) if s.enabled and s.auto]


def text_of(skill: store.Skill) -> str:
    return f"{skill.name.replace('-', ' ')}: {skill.description}"[:TEXT_CHARS]


async def vectors(conn, skills: list[store.Skill], spec: str) -> dict[str, list[float]]:
    """The vectors of the descriptions as they are now: one made with another embedder, or before the skill was
    last changed, is not among them."""
    rows = await (await conn.execute("select skill_id, stamp, vec from skill_vectors where spec=?", (spec,))).fetchall()
    stamps = {s.id: s.updated_at for s in skills}
    return {r["skill_id"]: list(_unpack(r["vec"])) for r in rows if stamps.get(r["skill_id"]) == r["stamp"]}


async def index(conn, *, stop=lambda: False) -> int:
    """Embed the descriptions that have no vector of their present words. Returns how many were embedded."""
    which = await spec(conn)
    skills = await candidates(conn)
    have = await vectors(conn, skills, which)
    missing = [s for s in skills if s.id not in have]
    if not missing:
        return 0
    embedder = embed.get(which, LOCK_TIMEOUT, stays_loaded=True)
    done = 0
    try:
        for at in range(0, len(missing), embed.EMBED_BATCH):
            if stop():
                break
            batch = missing[at:at + embed.EMBED_BATCH]
            made = await embedder.embed([text_of(s) for s in batch])
            await conn.executemany(
                "insert into skill_vectors(skill_id, spec, stamp, vec) values(?,?,?,?) on conflict(skill_id) do update "
                "set spec=excluded.spec, stamp=excluded.stamp, vec=excluded.vec",
                [(s.id, which, s.updated_at, _pack(v)) for s, v in zip(batch, made)])
            await conn.commit()
            done += len(batch)
    finally:
        await embedder.release()
    return done


async def run_job(conn, job, *, stop=lambda: False) -> str:
    """The worker's handler."""
    try:
        done = await index(conn, stop=stop)
    except EmbedError as e:
        await jobs.finish(conn, job.id, "failed", f"the descriptions of the skills could not be embedded: {e.message}"[:300])
        return "failed"
    await jobs.progress(conn, job.id, total=done, done=done)
    await jobs.finish(conn, job.id, "done")
    return "done"


def _age(stamp: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return math.inf


async def ask_for_index(conn) -> None:
    """Queue the job, unless one waits or runs, or one failed a moment ago (an embedder that is down is not asked
    again with every message)."""
    if await jobs.list(conn, kind=KIND, states=["queued", "running"], limit=1):
        return
    last = await jobs.list(conn, kind=KIND, limit=1)
    if last and last[0].state == "failed" and _age(last[0].finished_at) < RETRY_AFTER:
        return
    await jobs.enqueue(conn, KIND, {})


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _SMALL


def by_words(query: str, skills: list[store.Skill]) -> list[tuple[store.Skill, float]]:
    """The skills by how many words of the message their name and description share, by name where that is equal."""
    asked = _words(query)
    scored = [(s, float(len(asked & _words(text_of(s))))) for s in skills]
    return sorted(scored, key=lambda x: (-x[1], x[0].name))


async def fitting(conn, query: str, known: QueryVectors | None = None) -> Fit:
    """The skills to name for a message. `known`: what the run embedded so far; the message is ranked by meaning only
    when its vector is among it and every description has one."""
    skills = await candidates(conn)
    if len(skills) <= SHOWN:
        return Fit([(s, 0.0) for s in skills], len(skills), "all")
    which = await spec(conn)
    have = await vectors(conn, skills, which)
    if len(have) < len(skills):
        await ask_for_index(conn)
    query = (query or "").strip()[:QUERY_CHARS]
    asked = known.have(which, query) if known is not None and query else None
    if asked is not None and len(have) == len(skills):
        scored = sorted(((s, _cosine(asked, have[s.id])) for s in skills), key=lambda x: (-x[1], x[0].name))
        return Fit(scored[:SHOWN], len(skills), "meaning")
    return Fit(by_words(query, skills)[:SHOWN], len(skills), "words")
