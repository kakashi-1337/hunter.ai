"""Save points of a work folder: its files as they were at one moment, so that what a run changed can be listed
and put back.

A save point is a tree in a git folder of HunterAI's own (under the data folder, one per work folder), made with
the work folder as its work tree. The owner's own repository, when the work folder is one, is never touched: no
ref, no stash, no index entry. What the folder ignores (.gitignore) is not kept, so keys in a .env file and build
output are neither copied nor put back; a few folders that are always generated are left out too. Changes made
by commands count like changes made by the file tools: the whole folder is compared.

A repository inside the work folder (a project cloned into it) is kept file by file like any other folder; git
alone would keep a pointer to it and none of its files. Its own git folder is not read and not written.

Two save points of the same content are one and the same id, so a run that changes nothing leaves nothing behind.
Nothing is removed from the store by HunterAI: it grows by what runs change, and deleting it is safe (the undo
of earlier runs goes with it).

`take` returns None for a folder that cannot have a save point (no git, too broad, too large, not there) and the
run goes on without one; `before_run` keeps the reason for the owner."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import signal
import time
from pathlib import Path

from . import config

MAX_FILES = 50_000          # more files than this, the ignored ones not counted: no save point
MAX_BYTES = 1024**3         # more than this in those files: no save point
WALK_LIMIT = 400_000        # more files than this, the ignored ones counted: git is not even asked
MAX_SECONDS = 120.0         # for one git command
DEPTH = 12                  # repositories inside repositories inside the work folder, at most
ARGS_BYTES = 100_000        # of file names handed to git in one go: under what every system takes on a command line
ALWAYS_GENERATED = ("node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc", "target/", ".tox/", ".mypy_cache/",
                    ".pytest_cache/", ".ruff_cache/")
SEED = ".hunterai-seed"     # see _listed
WHY = "No save point was kept for this run: {}. What it changed cannot be listed or put back from here."
NOT_AT_END = "What this run changed could not be listed at its end: {}. Its changes cannot be put back from here."
GONE = ("The save point of this run is no longer kept (the folder {} was cleared, or is another one now), so nothing "
        "was put back.")
_SKIP = {p.rstrip("/") for p in ALWAYS_GENERATED if p.endswith("/")} | {".git"}
_KINDS = {"A": "new", "D": "deleted"}       # everything else that differs is "changed"
_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_locks: dict[str, asyncio.Lock] = {}
_runs: dict[str, tuple[Path, str, str]] = {}    # run -> (its work folder, the save point from before its first change, or why there is none)


class SavePointError(Exception):
    """A save point that was taken cannot be used now; the message says why, for the owner."""


class NoSavePoint(Exception):
    """Why this folder gets no save point: the words that go after "No save point was kept for this run: "."""


def store() -> Path:
    """Where every save point is kept. Only the owner can read it: it holds copies of the work folders' files."""
    p = config.home() / "save"
    p.mkdir(parents=True, exist_ok=True)
    if p.stat().st_mode & 0o077:
        os.chmod(p, 0o700)
    return p


def shadow(workdir: Path) -> Path:
    """HunterAI's own git folder for this work folder."""
    return config.home() / "save" / (hashlib.sha256(str(Path(workdir).resolve()).encode()).hexdigest()[:20] + ".git")


def _too_broad(workdir: Path) -> bool:
    """The home folder or above it, or a folder that HunterAI's own data is in (its save points would hold themselves)."""
    home, own = Path.home().resolve(), config.home().resolve()
    return (workdir == Path(workdir.anchor) or workdir in (home, own) or workdir in home.parents or workdir in own.parents
            or own in workdir.parents)


def _count(workdir: Path, limit: int) -> int:
    """Files in the folder, the always-generated folders left out; stops counting once past `limit`."""
    n = 0
    for _, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        n += len(files)
        if n > limit:
            break
    return n


def _size(n: int) -> str:
    return f"{n / 1024**3:g} GB" if n >= 1024**3 else f"{n / 1024**2:g} MB" if n >= 1024**2 else f"{n:,} bytes"


async def _end(proc) -> None:
    """End a git that is not waited for any longer, with whatever it started. It is asked first, so that it
    removes its own lock; what is left after that is killed (a lock it leaves is cleared later, see _ready).
    What it wrote is read to the end, so that nothing of it stays open."""
    def tell(sig) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)            # git runs in a session of its own, so this reaches it and its children only
    tell(signal.SIGTERM)
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), 3)
    tell(signal.SIGKILL)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.communicate(), 3)


async def _git(workdir: Path, *args: str, check: bool = True, feed: bytes | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_DIR=str(shadow(workdir)), GIT_WORK_TREE=str(workdir), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_LITERAL_PATHSPECS="1", LC_ALL="C")
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(workdir), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL if feed is None else asyncio.subprocess.PIPE, start_new_session=True)
    except OSError as e:
        raise SavePointError(f"git could not be run in {workdir}: {e.strerror or e}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(feed), MAX_SECONDS)
    except asyncio.TimeoutError:
        await _end(proc)
        raise SavePointError(f"git took longer than {MAX_SECONDS:g} seconds in {workdir}") from None
    except asyncio.CancelledError:      # the run was stopped: its git must not go on holding the lock of this folder
        await asyncio.shield(_end(proc))
        raise
    if check and proc.returncode != 0:
        raise SavePointError(f"git {args[0]} failed in {workdir}: {err.decode('utf-8', 'replace').strip()[:300]}")
    return out


def _names(out: bytes) -> list[str]:
    """Paths as git prints them with -z. A name that is no UTF-8 text comes out with U+FFFD in it and is never
    acted on (see undo)."""
    return [n for n in out.decode("utf-8", "replace").split("\0") if n]


async def _ready(workdir: Path) -> None:
    """The git folder of this work folder, made on first use."""
    own = shadow(workdir)
    if not (own / "HEAD").exists():
        store()
        await _git(workdir, "init", "-q")
        for key, value in (("core.autocrlf", "false"), ("core.quotePath", "false"), ("gc.auto", "0"),
                           ("advice.addEmbeddedRepo", "false")):
            await _git(workdir, "config", key, value)
        (own / "info").mkdir(exist_ok=True)
        (own / "info" / "exclude").write_text("\n".join(ALWAYS_GENERATED) + "\n")
    lock = own / "index.lock"
    try:        # left by a git that was killed (power, a kill -9); one that is at work now has a newer one
        if time.time() - lock.stat().st_mtime > MAX_SECONDS:
            lock.unlink()
    except OSError:
        pass


async def _listed(workdir: Path) -> list[str]:
    """Every file the save point will hold. A repository inside the folder shows up here as one name that ends
    in "/": git would keep a pointer to it (or fail, when it has no commit yet). An entry of ours below that
    name makes git walk it as the plain folder it also is; the entry itself goes away with the next `add`."""
    for _ in range(DEPTH):
        names = _names(await _git(workdir, "ls-files", "-z", "--cached", "--others", "--exclude-standard"))
        inner = [n for n in names if n.endswith("/")]
        if not inner:
            return names
        empty = (await _git(workdir, "hash-object", "-w", "--stdin", feed=b"")).decode().strip()
        await _git(workdir, "update-index", "-z", "--index-info",
                   feed=b"".join(f"100644 {empty}\t{n}{SEED}\0".encode() for n in inner))
    raise NoSavePoint(f"repositories lie inside repositories more than {DEPTH} deep in the work folder")


def _bytes(workdir: Path, names: list[str], limit: int) -> int:
    total = 0
    for n in names:
        try:
            total += os.lstat(workdir / n).st_size
        except (OSError, ValueError):
            continue
        if total > limit:
            break
    return total


async def _take(workdir: Path) -> str:
    """The folder as it is now, as a save point: its id. NoSavePoint when this folder cannot have one."""
    if not workdir.is_dir():
        raise NoSavePoint(f"the work folder {workdir} is not there")
    if _too_broad(workdir):
        raise NoSavePoint(f"the work folder {workdir} is your home folder, a folder above it, or one that holds HunterAI's own "
                          "data, which is too much to keep a copy of")
    if shutil.which("git") is None:
        raise NoSavePoint("git is not installed on this machine")
    if _count(workdir, WALK_LIMIT) > WALK_LIMIT:
        raise NoSavePoint(f"the work folder holds more than {WALK_LIMIT:,} files")
    async with _locks.setdefault(str(workdir), asyncio.Lock()):
        try:
            await _ready(workdir)
            names = await _listed(workdir)
            if len(names) > MAX_FILES:
                raise NoSavePoint(f"the work folder holds more than {MAX_FILES:,} files that are not ignored")
            if _bytes(workdir, names, MAX_BYTES) > MAX_BYTES:
                raise NoSavePoint(f"the work folder holds more than {_size(MAX_BYTES)} in files that are not ignored")
            await _git(workdir, "add", "-A", ".")
            return (await _git(workdir, "write-tree")).decode().strip()
        except (SavePointError, OSError) as e:
            raise NoSavePoint(str(e).rstrip(".")) from None


async def take(workdir) -> str | None:
    """The folder as it is now, as a save point: its id, or None when this folder cannot have one."""
    try:
        return await _take(Path(workdir).resolve())
    except NoSavePoint:
        return None


async def _exists(workdir: Path, found: str) -> bool:
    try:
        await _git(workdir, "cat-file", "-e", found + "^{tree}")
    except SavePointError:
        return False
    return True


async def _kept(workdir: Path, *ids: str) -> None:
    """Each id must be a save point that the store of this folder still holds; nothing else is handed to git."""
    for found in ids:
        if not _ID.fullmatch(found or "") or not (shadow(workdir) / "HEAD").exists() or not await _exists(workdir, found):
            raise SavePointError(GONE.format(config.home() / "save"))


async def changed(workdir, before: str, after: str) -> list[dict]:
    """What differs between two save points: [{"path", "change": "new" | "changed" | "deleted"}], by path."""
    workdir = Path(workdir).resolve()
    if not workdir.is_dir():
        raise SavePointError(f"The work folder {workdir} is not there any more, so nothing was put back.")
    await _kept(workdir, before, after)
    parts = (await _git(workdir, "diff", "--name-status", "--no-renames", "-z", before, after)).decode("utf-8", "replace").split("\0")
    found = [{"path": path, "change": _KINDS.get(kind[:1], "changed")} for kind, path in zip(parts[::2], parts[1::2]) if path]
    return sorted(found, key=lambda f: f["path"])


async def _entries(workdir: Path, tree: str) -> dict[str, str]:
    """path -> "mode blob" at a save point."""
    found = {}
    for entry in (await _git(workdir, "ls-tree", "-r", "-z", tree)).decode("utf-8", "replace").split("\0"):
        if "\t" in entry:
            meta, path = entry.split("\t", 1)
            found[path] = " ".join(meta.split()[::2])       # mode and object id; the type in the middle says nothing more
    return found


def _in_the_way(workdir: Path, path: str) -> bool:
    """Would bringing this file back replace something that is there now? A folder where the file was (with
    whatever is in it), or a file or a link where one of its folders was."""
    file = workdir / path
    if file.is_dir() and not file.is_symlink():
        return True
    return any(p.is_symlink() or (p.exists() and not p.is_dir()) for p in file.parents if workdir in p.parents)


def _goes(paths: list[str]):
    """The paths in lots that each fit on one command line."""
    some, size = [], 0
    for path in paths:
        n = len(path.encode()) + 1
        if some and size + n > ARGS_BYTES:
            yield some
            some, size = [], 0
        some.append(path)
        size += n
    if some:
        yield some


async def undo(workdir, before: str, after: str) -> dict:
    """Put back what changed between two save points, file by file. A file that is not as the later save point
    left it (the owner worked on it since) is left alone and named; one that is already as it was needs nothing.
    Nothing that is there now is replaced unseen: where the owner's work is in the way, the file is left too.
    {"restored": [paths], "left": [paths]}"""
    workdir = Path(workdir).resolve()
    paths = [f["path"] for f in await changed(workdir, before, after)]
    if not paths:
        return {"restored": [], "left": []}
    try:
        now = await _take(workdir)
    except NoSavePoint as e:
        raise SavePointError(f"The work folder cannot be read as it is now ({e}), so nothing was put back.") from None
    was, left_as, is_now = [await _entries(workdir, t) for t in (before, after, now)]
    made, back, left = [], [], []
    for path in paths:
        if "\ufffd" in path:
            left.append(path)                               # a name that is no text: not guessed at
        elif is_now.get(path) == was.get(path):
            continue                                        # already as it was
        elif is_now.get(path) != left_as.get(path):
            left.append(path)                               # changed since the run: the owner's own work
        else:
            (back if path in was else made).append(path)
    async with _locks.setdefault(str(workdir), asyncio.Lock()):
        for path in sorted(made, reverse=True):             # what the run made goes first, so that a folder it made can make room
            file = workdir / path
            file.unlink(missing_ok=True)
            for folder in file.parents:                     # a folder the run made for it goes with it, when nothing else is in it
                if folder == workdir or any(folder.iterdir()):
                    break
                folder.rmdir()
        clear = [p for p in back if not _in_the_way(workdir, p)]
        left += [p for p in back if p not in clear]
        try:
            for some in _goes(clear):
                await _git(workdir, "checkout", before, "--", *some)
        except SavePointError as e:
            raise SavePointError(f"{str(e).rstrip('.')}. Some of the files may be back already: Undo again goes on "
                                 "with the rest.") from None
    return {"restored": sorted(made + clear), "left": sorted(left)}


# ---- the save point of a run

async def before_run(run_id: str, workdir) -> None:
    """Called before a run changes anything (a file tool, a command): the folder as it is now is kept, once
    for the run."""
    if run_id in _runs:
        return
    folder = Path(workdir)
    try:
        folder = folder.resolve()
        kept = (folder, await _take(folder), "")
    except NoSavePoint as e:
        kept = (folder, "", WHY.format(e))
    except Exception as e:  # noqa: BLE001  a fault here must not stop the call it stands before
        kept = (folder, "", WHY.format(f"{type(e).__name__}: {e}"[:300].rstrip(".")))
    _runs.setdefault(run_id, kept)


async def after_run(run_id: str) -> dict | None:
    """At the end of a run that was about to change something: {"folder", "before", "after", "files", "reason"}.
    `files` is what differs now; `reason` says, for the owner, why there is no list (no save point at the
    start, or none at the end). None for a run that never came to that point."""
    kept = _runs.pop(run_id, None)
    if kept is None:
        return None
    folder, before, reason = kept
    found = {"folder": str(folder), "before": before, "after": "", "files": [], "reason": reason}
    if before:
        try:
            after = await _take(folder)
            found.update(after=after, files=await changed(folder, before, after))
        except Exception as e:  # noqa: BLE001  NoSavePoint and SavePointError say why in words; anything else by its name
            said = str(e) if isinstance(e, (NoSavePoint, SavePointError)) else f"{type(e).__name__}: {e}"[:300]
            found.update(after="", files=[], reason=NOT_AT_END.format(said.rstrip(".")))
    return found
