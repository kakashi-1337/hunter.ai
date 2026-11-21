"""Where ingest input comes from: files uploaded into a staging folder, or a folder (or one
file) on this machine. Discovery names every file found and every file passed over, with why."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import stat
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import config, db
from . import archives, generated, limits, parsers
from .archives import Found, Problem
from .limits import Budget

IGNORED_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox",
                          ".mypy_cache", ".pytest_cache", ".idea", ".vscode"})
# Never read: their text would end up in prompts sent to cloud models.
SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*",
                   "id_ed25519*", ".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc", ".htpasswd",
                   "*.p12", "*.pfx", "*.jks", "*.keystore", "credentials", "credentials.json")
OWN_DATA = "HunterAI's own data folder (not ingested)"
BLOCK = 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{32}$")
_locks: dict[str, asyncio.Lock] = {}


@dataclass
class StagingInfo:
    collection_id: str
    files: int
    bytes: int
    job_id: str | None
    created_at: str


@dataclass
class Staged:
    path: str
    size: int
    rejected: str | None


def is_secret(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), p) for p in SECRET_PATTERNS)


def _passed_over(directory: str, screened: bool) -> str | None:
    """Why a walk does not enter a directory of this name, or None. `screened`: what nobody wrote is left out too."""
    if directory in IGNORED_DIRS:
        return "ignored directory"
    return generated.BUILD if screened and directory in generated.DIRS else None


def _screen_members(uri: str, found: list[Found], screened: bool) -> tuple[list[Found], list[Problem]]:
    """The rules a folder walk applies, applied to what came out of an archive: nothing under
    an ignored directory and no secret-like file is kept, on disk or in the index; with `screened`,
    nothing that nobody wrote either."""
    keep: list[Found] = []
    problems: list[Problem] = []
    pruned: set[str] = set()
    for f in found:
        parts = f.uri.split("!/")
        ignored = why = None
        for depth, member in enumerate(parts[1:], 1):
            dirs = member.split("/")[:-1] if depth == len(parts) - 1 else member.split("/")
            for i, d in enumerate(dirs):
                why = _passed_over(d, screened)
                if why:
                    ignored = "!/".join(parts[:depth]) + "!/" + "/".join(member.split("/")[:i + 1]) + "/"
                    break
            if ignored:
                break
        if ignored:
            if ignored not in pruned:
                pruned.add(ignored)
                problems.append(Problem(ignored, "skipped", why))
        elif is_secret(f.path.name):
            problems.append(Problem(f.uri, "skipped", "secret-like file name (not ingested)"))
        elif screened and (named := generated.by_name(f.path.name)):
            problems.append(Problem(f.uri, "skipped", named))
        else:
            keep.append(f)
            continue
        f.path.unlink(missing_ok=True)
    return keep, problems


def _root(staging_id: str) -> Path:
    if not isinstance(staging_id, str) or not _ID.match(staging_id):
        raise ValueError("not a staging id")
    return config.staging_dir() / staging_id


def staging_path(staging_id: str) -> Path:
    root = _root(staging_id)
    if not root.is_dir():
        raise FileNotFoundError(staging_id)
    return root


def _meta(root: Path) -> dict:
    return json.loads((root / "meta.json").read_text())


def _write_meta(root: Path, meta: dict) -> None:
    tmp = root / "meta.json.tmp"
    tmp.write_text(json.dumps(meta))
    os.replace(tmp, root / "meta.json")


def new_staging(collection_id: str) -> str:
    limits.require_space()
    sid = db.new_id()
    root = _root(sid)
    (root / "incoming").mkdir(parents=True)
    _write_meta(root, {"collection_id": collection_id, "files": {}, "job_id": None, "created_at": db.now()})
    return sid


def staging_info(staging_id: str) -> StagingInfo:
    m = _meta(staging_path(staging_id))
    return StagingInfo(m["collection_id"], len(m["files"]), sum(m["files"].values()), m.get("job_id"), m["created_at"])


def mark_committed(staging_id: str, job_id: str) -> None:
    root = staging_path(staging_id)
    _write_meta(root, {**_meta(root), "job_id": job_id, "committed_at": db.now()})


def remove_staging(staging_id: str) -> None:
    shutil.rmtree(_root(staging_id), ignore_errors=True)
    _locks.pop(staging_id, None)


def _lock(staging_id: str) -> asyncio.Lock:
    return _locks.setdefault(staging_id, asyncio.Lock())


def _note_rejection(root: Path, path: str, reason: str) -> None:
    with open(root / "rejected.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"path": path[:1024], "reason": reason}) + "\n")


async def reject(staging_id: str, relpath: str, reason: str) -> None:
    """Record a file that never arrived. `relpath` is a label only, never used as a path."""
    async with _lock(staging_id):
        _note_rejection(staging_path(staging_id), relpath, reason)


async def stage_file(staging_id: str, relpath: str, read: Callable[[int], Awaitable[bytes]]) -> Staged:
    async with _lock(staging_id):
        root = staging_path(staging_id)
        meta = _meta(root)
        rel = archives.safe_member(relpath)

        def refuse(reason: str) -> Staged:
            _note_rejection(root, relpath, reason)
            return Staged(relpath, 0, reason)
        if rel is None:
            return refuse("unsafe path")
        files: dict[str, int] = meta["files"]
        old = files.pop(rel, None)              # re-sending a path replaces the file
        budget = Budget(len(files), sum(files.values()))
        why = budget.refusal(0, stored=True)
        if why:
            if old is not None:
                files[rel] = old
            return refuse(why)
        room, room_why = budget.room()
        dest = root / "incoming" / rel
        if dest.is_dir() or any(p.is_file() for p in dest.parents if (root / "incoming") in p.parents):
            if old is not None:
                files[rel] = old
            return refuse("path collides with another uploaded file")
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with open(dest, "wb") as f:
                while True:
                    block = await read(BLOCK)
                    if not block:
                        break
                    size += len(block)
                    if size > room:
                        break
                    f.write(block)
        except BaseException:
            dest.unlink(missing_ok=True)
            _write_meta(root, meta)
            raise
        if size > room:
            dest.unlink(missing_ok=True)
            _write_meta(root, meta)
            return refuse(room_why)
        files[rel] = size
        _write_meta(root, meta)
        return Staged(rel, size, None)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b
    except OSError:
        return False


def _age(stamp: str | None) -> float:
    if not stamp:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()


def sweep_staging(keep: set[str], max_age_seconds: float = 86400, min_committed_age: float = 60.0) -> int:
    """Remove staging folders nothing needs: committed ones whose job is gone, and uploads
    that were started and never committed."""
    removed = 0
    base = config.staging_dir()
    for d in base.iterdir():
        if not d.is_dir() or d.name in keep:
            continue
        try:
            meta = _meta(d)
        except (OSError, ValueError):
            meta = {}
        committed = meta.get("committed_at")
        if (committed and _age(committed) > min_committed_age) or \
                (not committed and _age(meta.get("created_at")) > max_age_seconds):
            shutil.rmtree(d, ignore_errors=True)
            _locks.pop(d.name, None)
            removed += 1
    return removed


async def discover(payload: dict, *, skip_generated: bool = True) -> AsyncIterator[Found | Problem]:
    """Every file of the job's source, and every file or folder passed over with the reason.
    Sources: "upload" (staged files), "folder" (read in place), "git" (a shallow clone into
    staging), "web" (a bounded crawl into staging). `skip_generated`: what nobody wrote is passed over too
    (kb/generated.py: build output, a minified or bundled file, a source map, a lock file), wherever a
    folder, a repository or an archive brings it along. The pages of a crawl were asked for and are not judged."""
    root = staging_path(payload["staging_id"])
    unpacked = root / "unpacked"
    source = payload["source"]
    kind = source["kind"]
    budget = Budget()
    if kind == "web":
        from . import crawl
        shutil.rmtree(root / "web", ignore_errors=True)
        try:
            async for x in crawl.crawl(source, root / "web", budget):
                yield x
        except ValueError as e:
            yield Problem(str(source.get("url")), "failed", str(e))
        return
    in_place = kind == "folder"
    if kind == "upload":
        meta = _meta(root)
        budget = Budget(len(meta["files"]), sum(meta["files"].values()))
        rejected = root / "rejected.jsonl"
        if rejected.exists():
            for line in rejected.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                yield Problem("upload:" + r["path"], "skipped", r["reason"])
        base, prefix = root / "incoming", "upload:"
    elif kind == "git":
        from . import gitsrc
        try:
            url, label = gitsrc.validate(source["url"])
            await gitsrc.clone(url, root / "repo", limits.MAX_JOB_BYTES)
        except (ValueError, gitsrc.GitError) as e:
            yield Problem(str(source.get("url")), "failed", str(e))
            return
        base, prefix = root / "repo", f"git:{label}/"
    else:
        base, prefix = Path(source["path"]), ""

    def uri_of(p: Path) -> str:
        return str(p) if in_place else prefix + p.relative_to(base).as_posix()

    async def one(p: Path) -> AsyncIterator[Found | Problem]:
        uri = uri_of(p)
        try:
            st = os.lstat(p)
        except OSError as e:
            yield Problem(uri, "failed", f"cannot read: {type(e).__name__}")
            return
        counted = kind != "upload"       # uploads were counted when they were staged
        if stat.S_ISLNK(st.st_mode):
            yield Problem(uri, "skipped", "symlink not followed")
        elif not stat.S_ISREG(st.st_mode):
            yield Problem(uri, "skipped", "special file")
        elif is_secret(p.name):
            yield Problem(uri, "skipped", "secret-like file name (not ingested)")
        elif skip_generated and (named := generated.by_name(p.name)):
            yield Problem(uri, "skipped", named)        # before the size is asked for: it uses up nothing of the job
        elif counted and (why := budget.refusal(st.st_size, stored=not in_place)):
            yield Problem(uri, "skipped", why)
        elif parsers.kind_for(p.name) == "archive":
            found, problems = await archives.unpack(p, uri, unpacked, budget)
            found, more = _screen_members(uri, found, skip_generated)
            for x in (*found, *problems, *more):
                yield x
        else:
            if counted:
                budget.add(st.st_size, stored=not in_place)
            yield Found(uri, p, st.st_size)

    own = config.home().resolve()
    if in_place and not base.is_dir():
        async for x in one(base):
            yield x
        return
    for dirpath, dirnames, names in os.walk(base, followlinks=False):
        here = Path(dirpath)
        for d in sorted(dirnames):
            if in_place and _same(here / d, own):
                yield Problem(uri_of(here / d) + "/", "skipped", OWN_DATA)
            elif (why := _passed_over(d, skip_generated)):
                yield Problem(uri_of(here / d) + "/", "skipped", why)
            elif (here / d).is_symlink():
                yield Problem(uri_of(here / d), "skipped", "symlink not followed")
        dirnames[:] = sorted(d for d in dirnames if not _passed_over(d, skip_generated) and not (here / d).is_symlink()
                             and not (in_place and _same(here / d, own)))
        for n in sorted(names):
            async for x in one(here / n):
                yield x
