"""Documents that were read from a folder of this machine and whose file is gone from it. A folder that is indexed
again takes in what is new and what changed, and leaves the documents of the files that were deleted since. Here
they are found, and taken out when the owner says so. Only what was read in place is looked at: a repository, an
upload or a page has no file on this machine to look for."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

from ..repo import jobs, kb

NAMED = 20          # documents that an answer names; all of them are counted


@dataclass
class Found:
    checked: int = 0                                                # documents that were read from a folder
    gone: list[tuple[str, str]] = field(default_factory=list)      # (id, address) of those whose file is gone, by address
    away: int = 0                                                   # documents of a source that is not there now
    sources: list[str] = field(default_factory=list)                # those sources
    unknown: int = 0                                                # documents whose folder stands in no queue entry: not judged


def _sources_of(path: str, sources: list[str]) -> list[str]:
    """The folders or files the owner added that this file came with: each one that is it or holds it, the outermost first."""
    return sorted((s for s in sources if path == s or path.startswith(s.rstrip(os.sep) + os.sep)), key=len)


def _look(docs: list[tuple[str, str]], sources: list[str]) -> Found:
    found, there, away = Found(checked=len(docs)), {}, set()

    def exists(path: str) -> bool:
        if path not in there:
            there[path] = os.path.exists(path)
        return there[path]

    for did, uri in sorted(docs, key=lambda d: d[1]):
        path = uri.split("!/", 1)[0]                # a member of an archive is gone with its archive
        held = _sources_of(path, sources)
        if not held:
            found.unknown += 1
        elif not any(exists(s) for s in held):      # a drive that is not plugged in, a folder that was moved: its files are not judged
            found.away += 1
            away.add(held[0])
        elif not exists(path):
            found.gone.append((did, uri))
    found.sources = sorted(away)
    return found


async def look(conn, collection_id: str) -> Found:
    """What is gone of a collection's folders. A source is what a queue entry of the collection names: a document
    whose folder stands in none (its entry was removed) cannot be told from one of a drive that is not plugged in,
    and is not judged."""
    docs = await kb.folder_documents(conn, collection_id)
    added = await jobs.list(conn, kind="ingest", limit=None, payload_eq={"collection_id": collection_id, "source.kind": "folder"})
    sources = sorted({str(j.payload["source"]["path"]) for j in added})
    return await asyncio.to_thread(_look, docs, sources)
