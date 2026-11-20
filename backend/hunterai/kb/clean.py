"""Take out of a collection what would be left out of it today (scripts/kb-clean.sh).

A collection that was filled before build output, bundles, source maps and lock files were left out still holds
them: indexing a folder again does not look into a directory it now passes over. This goes through what the
collection holds and judges each document by the rules a job judges a file by (kb/generated.py): the directory it
stood in, its name, and for a script or a style sheet its shape, read from its passages.

It says what it found and changes nothing. Asked to (--apply), it takes them out the way a job sets a file aside:
their vectors go, then their passages, and each stays listed in the collection as skipped, with the reason. The
files themselves are never touched. That cannot be undone here: to have one back, the switch that leaves such
files out is turned off and its folder indexed again."""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import config, db
from ..repo import jobs, kb, settings
from ..types import Collection, Document
from . import generated, pipeline
from .vectors import VectorError, get_store

ACTIVE = ["queued", "running", "paused"]
JOBS = ("ingest", "reembed")    # the jobs that write to a collection
PLACES_SHOWN = 8
LARGEST_SHOWN = 5
PLACE_DEPTH = 4                 # directories of a path on this machine that name the place a file stood in
ORDER = (generated.BUILD, generated.MINIFIED, generated.LONG_LINES, generated.SOURCE_MAP, generated.LOCK)


@dataclass
class Entry:
    document: Document
    reason: str
    passages: int


def _now_and_then(n: int, total: int, every: int) -> bool:
    return n % every == 0 or n == total


async def plan(conn, collection: Collection, say=lambda text: None) -> list[Entry]:
    """Every document the collection searches that a job would leave out today, with the reason. A page of a crawled
    site is never one of them."""
    documents = [d for d in await kb.indexed_documents(conn, collection.id) if d.source_kind != "web"]
    entries = []
    for n, doc in enumerate(documents, 1):
        if _now_and_then(n, len(documents), 5000):
            say(f"looked at {n:,} of {len(documents):,} files")
        why = generated.of_uri(doc.source_kind, doc.source_uri)
        name = doc.source_uri.rsplit("/", 1)[-1]
        if why is None and generated.has_shape(name):
            why = generated.by_shape(name, (text.encode() for text in await kb.chunk_texts(conn, doc.id)))
        if why:
            entries.append(Entry(doc, why, doc.chunk_count))
    return entries


async def apply(conn, collection: Collection, entries: list[Entry], *, store=None,
                say=lambda text: None) -> tuple[list[Entry], str | None]:
    """Take them out, one whole document at a time: (the ones that went, why it stopped before the end or None).
    It stops at a vector store that cannot be reached. The document it stopped at is as it was: a document's
    vectors go first, and its passages only after them."""
    store = store or get_store()
    done: list[Entry] = []
    for n, e in enumerate(entries, 1):
        doc = e.document
        try:
            await pipeline.set_aside(conn, store, collection, doc.source_kind, doc.source_uri, "skipped", e.reason,
                                     job_id=doc.job_id, sha256=doc.sha256, size=doc.size)
        except VectorError as error:
            return done, str(error)
        done.append(e)
        if _now_and_then(n, len(entries), 500):
            say(f"taken out {n:,} of {len(entries):,} files")
    return done, None


def place(doc: Document) -> str:
    """About where a document came from: the repository, or the first directories of its path on this machine,
    as far as the build directory it stood in."""
    if doc.source_kind == "git":
        return "/".join(doc.source_uri.split("/")[:3])
    if doc.source_kind == "upload":
        return "uploaded files"
    directories = [p for p in doc.source_uri.split("!/")[0].split("/") if p][:-1]
    built = next((i for i, d in enumerate(directories) if d in generated.DIRS), len(directories))
    return "/" + "/".join(directories[:min(built, PLACE_DEPTH)])


def _label(reason: str) -> str:
    return reason.removesuffix(generated.SUFFIX)


def _files(n: int) -> str:
    return f"{n:,} file" if n == 1 else f"{n:,} files"


def _sums(entries: list[Entry], key) -> list[tuple[str, int, int]]:
    """(key, files, passages), the most passages first."""
    sums: dict[str, list[int]] = {}
    for e in entries:
        s = sums.setdefault(key(e), [0, 0])
        s[0] += 1
        s[1] += e.passages
    return sorted(((k, f, p) for k, (f, p) in sums.items()), key=lambda x: (-x[2], x[0]))


def report(collection: Collection, counts: dict, entries: list[Entry]) -> str:
    """What was found, in words: how much, of which kind, where it stands, and the largest."""
    files, passages = counts["documents"]["ok"], counts["chunks"]
    head = f'Collection "{collection.name}": {passages:,} passages in {files:,} files.'
    if not entries:
        return (f"{head}\nThere is nothing that would be left out of it today: no build output, no minified or bundled "
                "file, no source map, no lock file.")
    gone = sum(e.passages for e in entries)
    share = f"{100 * gone / passages:.1f}%" if passages else "0%"
    lines = [head, f"As indexing is set now, {len(entries):,} of these files would be left out: {gone:,} passages, "
                   f"{share} of the collection.", ""]
    kinds = {k: (f, p) for k, f, p in _sums(entries, lambda e: e.reason)}
    width = max(len(_label(k)) for k in kinds) + 2
    lines.append(f"{'':<{width}}{'files':>9}{'passages':>11}")
    lines += [f"{_label(k):<{width}}{kinds[k][0]:>9,}{kinds[k][1]:>11,}" for k in ORDER if k in kinds]
    places = _sums(entries, lambda e: place(e.document))
    lines += ["", "Where they stand:"]
    lines += [f"  {_files(f):>13} {p:>9,} passages   {k}" for k, f, p in places[:PLACES_SHOWN]]
    if len(places) > PLACES_SHOWN:
        rest = places[PLACES_SHOWN:]
        lines.append(f"  {_files(sum(f for _, f, _ in rest)):>13} {sum(p for _, _, p in rest):>9,} passages   "
                     f"in {len(rest)} more place{'' if len(rest) == 1 else 's'}")
    lines += ["", "The largest:"]
    lines += [f"  {e.passages:>7,} passages   {e.document.source_uri}   ({_label(e.reason)})"
              for e in sorted(entries, key=lambda e: (-e.passages, e.document.source_uri))[:LARGEST_SHOWN]]
    return "\n".join(lines)


def keep_list(collection: Collection, entries: list[Entry], taken_out: bool) -> Path:
    """Every one of them in a file of the data folder, so that the whole list can be read, also afterwards."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.home() / "kb-clean" / f"{collection.id}-{stamp}.txt"
    what = "what was taken out of it" if taken_out else "what would be left out of it today (nothing was changed)"
    rows = [f'# Collection "{collection.name}", {db.now()}: {what}.', "# passages\twhy\twhere it came from"]
    rows += [f"{e.passages}\t{_label(e.reason)}\t{e.document.source_uri}" for e in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n")
    return path


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kb-clean", description="What a collection holds that would be left out of it today.")
    parser.add_argument("collection", nargs="?", help="the name of a collection; left out: the collections are listed")
    parser.add_argument("--apply", action="store_true", help="take them out of the collection (without it, nothing is changed)")
    args = parser.parse_args(argv)
    progress = lambda text: print(f"  {text}", file=sys.stderr, flush=True)       # noqa: E731
    conn = await db.connect()
    try:
        await db.migrate(conn)          # as every command does: a data folder that was never used has no tables yet
        hidden = await settings.get(conn, "memory_collection_id")      # memory is managed under Settings, not here
        collections = [c for c in await kb.list_collections(conn) if c.id != hidden]
        names = ", ".join(f'"{c.name}"' for c in collections) or "none yet"
        if not args.collection:
            print(f"Collections: {names}.\nRun it with the name of one: scripts/kb-clean.sh \"NAME\"")
            return 0
        collection = next((c for c in collections if args.collection in (c.name, c.id)), None)
        if collection is None:
            print(f'There is no collection named "{args.collection}". Collections: {names}.')
            return 1
        if not await settings.get(conn, "kb_skip_generated", True):
            print("Leaving out build output, bundles, source maps and lock files is switched off (Settings, Knowledge "
                  f'base), so nothing would be left out of "{collection.name}" and nothing is taken out. Switch it on, '
                  "then run this again.")
            return 1
        if args.apply:
            busy = [j for j in await jobs.list(conn, states=ACTIVE, payload_eq={"collection_id": collection.id}, limit=None)
                    if j.kind in JOBS]
            if busy:
                print(f'"{collection.name}" has a job that is queued or running (indexing, or embedding again). Wait for '
                      "it to finish, or cancel it, then run this again. Nothing was changed.")
                return 1
        entries = await plan(conn, collection, say=progress)
        print(report(collection, await kb.counts(conn, collection.id), entries))
        if not entries:
            return 0
        if not args.apply:
            print(f"The whole list: {keep_list(collection, entries, taken_out=False)}")
            print("\nNothing was changed. To take them out of the collection:\n"
                  f'  scripts/kb-clean.sh "{collection.name}" --apply\n'
                  "That removes their passages and their vectors for good. The files themselves are not touched, and "
                  "each stays listed in the collection as skipped, with the reason. To have one back: switch off "
                  '"Leave out what nobody wrote" in Settings (Knowledge base) and index its folder again.')
            return 0
        done, failed = await apply(conn, collection, entries, say=progress)
        print()
        if failed is not None:
            print(f"The vector store could not be reached ({failed}). {_files(len(done))} had been taken out before that; "
                  "the rest is as it was. Start it (scripts/qdrant.sh), then run this again.")
        if done:
            left = (await kb.counts(conn, collection.id))["chunks"]
            print(f'Taken out of "{collection.name}": {_files(len(done))}, {sum(e.passages for e in done):,} passages. '
                  f"It now has {left:,} passages.")
            print(f"What was taken out is listed in {keep_list(collection, done, taken_out=True)}")
            if (config.home() / "rag-tests" / f"{collection.id}.json").exists():
                print("The kept search test of this collection asks about passages that may be gone now. A new one, from "
                      f'what the collection holds: scripts/rag-test.sh "{collection.name}" --new')
        return 0 if failed is None else 1
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
