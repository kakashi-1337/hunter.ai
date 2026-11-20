"""Bring this folder to the newest HunterAI: `scripts/update.sh`.

One command stops HunterAI, fetches the new code, changes the files, and starts HunterAI again.
What the owner changed in the folder is kept: a file changed here and in the update is merged
when the changes do not meet, and when one cannot be merged nothing at all is changed.

How the files are changed. The folder is taken as it stands (only the files HunterAI itself
has; the owner's programs in bin/ and anything else beside them are never read), and git
merges that with the new version on top of the version the folder was copied from, without
touching a file (`git merge-tree`). Only a result without conflicts is written to disk, in
one step, after HunterAI was stopped. The branch then stands on the fetched commit, so that
`git status` and `git diff` show the owner's own changes and nothing else.

A folder with commits of its own (--reanchor) has no version on record that it was copied
from, and it need not be one version: some files can be newer than others. So the starting
point is found file by file. A file that is a copy of any version the branch had is not the
owner's and is simply brought to the newest; a file that matches no version is the owner's
own, and is merged on top of the version it is closest to.

Standard library and git only, and no import from the rest of HunterAI: this module is what
replaces the rest.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal as signals
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

START = 10                      # exit code: the folder was updated; scripts/update.sh runs the start script next
SOURCE = "https://github.com/kakashi-1337/hunter.ai.git"
BRANCH = "main"
NEW = "refs/hunterai/new"       # the fetched commit
FAR = 40                        # a folder with more files that match no version of the branch is no copy of it
LOOK_BACK = 500                 # how many commits of the branch are searched for the versions of a file
NOBODY = {"GIT_AUTHOR_NAME": "HunterAI update", "GIT_AUTHOR_EMAIL": "update@hunterai.invalid",
          "GIT_COMMITTER_NAME": "HunterAI update", "GIT_COMMITTER_EMAIL": "update@hunterai.invalid"}


class Refused(Exception):
    """The update cannot start, and nothing was changed. The text is for the owner."""


@dataclass
class Proc:
    pid: int
    kind: str                   # "server" or "worker"


@dataclass
class Outcome:
    status: str                 # current | updated | conflicts | diverged | ahead | foreign
    applied: bool = False       # whether the folder was changed
    old: str = ""               # the version of the branch the folder stood at; empty when it was no single one
    brought: list[str] = field(default_factory=list)       # the files that change on disk
    new: str = ""               # the newest version of the branch
    commits: list[str] = field(default_factory=list)       # what is new, newest first
    merged: list[str] = field(default_factory=list)        # changed here and in the update, and merged
    conflicts: list[str] = field(default_factory=list)     # changed here and in the update, where the changes meet
    aside: list[str] = field(default_factory=list)         # the owner's version is beside the new one as <file>.mine
    kept: list[str] = field(default_factory=list)          # files that hold a change of the owner's after the update
    saved: Path | None = None   # copies of the owner's versions from before
    own_commits: int = 0        # commits of the folder that the branch does not have
    was: str = ""               # the commit the folder's branch stood on
    apart: int = 0              # for a folder that is no copy of the branch: its files that match no version of it


class _Git:
    def __init__(self, root: Path):
        self.root = root

    def __call__(self, *args: str, env: dict | None = None, input: bytes | None = None, ok=(0,)) -> str:
        done = subprocess.run(["git", *args], cwd=self.root, env={**os.environ, **(env or {})}, input=input, capture_output=True)
        if done.returncode not in ok:
            raise Refused(f"git {args[0]} failed: {done.stderr.decode(errors='replace').strip() or done.returncode}")
        self.code = done.returncode
        return done.stdout.decode(errors="surrogateescape")

    def true(self, *args: str) -> bool:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True).returncode == 0

    def names(self, *args: str, env: dict | None = None) -> list[str]:
        return [n for n in self(*args, env=env).split("\0") if n]

    def files(self, tree: str) -> list[str]:
        return self.names("ls-tree", "-r", "-z", "--name-only", tree)

    def differing(self, a: str, b: str) -> list[str]:
        return self.names("diff-tree", "-r", "-z", "--name-only", "--no-renames", a, b)


def _checkout(root: Path) -> _Git:
    git = _Git(root)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
    except FileNotFoundError:
        raise Refused("git is not installed: the update needs it.") from None
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != root.resolve():
        raise Refused(f"{root} is not a git checkout of HunterAI, so it cannot be updated in place. "
                      "Clone it with git, or download the new version and copy your .env over.")
    version = tuple(int(x) for x in git("version").split()[2].split(".")[:2] if x.isdigit())
    if version < (2, 38):
        raise Refused(f"git {'.'.join(map(str, version))} is too old for the update: it needs 2.38 or newer.")
    for mark in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        if (root / git("rev-parse", "--git-path", mark).strip()).exists():
            raise Refused("A merge or rebase was left unfinished in this folder. Finish it or abort it with git, then update.")
    return git


class _Folder:
    """The folder as it stands, as a tree git can merge: for the given paths, what is on disk now."""

    def __init__(self, git: _Git, index: str):
        self.git, self.env = git, {"GIT_INDEX_FILE": index}

    def take(self, paths: list[str]) -> str:
        if os.path.exists(self.env["GIT_INDEX_FILE"]):
            os.unlink(self.env["GIT_INDEX_FILE"])
        self.git("update-index", "--add", "--remove", "-z", "--stdin", env=self.env,
                 input="\0".join(paths).encode(errors="surrogateescape"))
        return self.git("write-tree", env=self.env).strip()

    def entries(self, tree: str) -> dict[str, tuple[str, str]]:
        """path -> (mode, blob) for every file of a tree."""
        found = {}
        for line in self.git.names("ls-tree", "-r", "-z", tree):
            meta, path = line.split("\t", 1)
            mode, _, blob = meta.split(" ")
            found[path] = (mode, blob)
        return found

    def tree(self, entries: dict[str, tuple[str, str]], index: str) -> str:
        """The tree that holds exactly these files."""
        env = {"GIT_INDEX_FILE": index}
        if os.path.exists(index):
            os.unlink(index)
        lines = "".join(f"{mode} {blob}\t{path}\0" for path, (mode, blob) in entries.items())
        self.git("update-index", "-z", "--index-info", env=env, input=lines.encode(errors="surrogateescape"))
        return self.git("write-tree", env=env).strip()

    def without(self, paths: list[str], base: str) -> str:
        """The same tree with these paths as `base` has them: the owner's change to them left out."""
        for path in paths:
            entry = self.git("ls-tree", "-z", base, "--", path).split("\0")[0]
            if entry:
                mode, _, blob = entry.split("\t")[0].split(" ")
                self.git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=self.env)
            else:
                self.git("update-index", "--force-remove", "--", path, env=self.env)
        return self.git("write-tree", env=self.env).strip()


def _merge(git: _Git, folder_tree: str, base: str, new: str) -> tuple[str, list[str]]:
    """(the tree of the folder with the update in it, the files where the two could not be merged).
    `base` is a commit that `new` descends from: both sides are compared with it."""
    ours = git("commit-tree", folder_tree, "-p", base, "-m", "the folder as it stands", env=NOBODY).strip()
    parts = git("merge-tree", "--write-tree", "--name-only", "-z", ours, new, ok=(0, 1)).split("\0")
    conflicts = []
    for name in parts[1:]:
        if not name:
            break
        conflicts.append(name)
    return parts[0], sorted(set(conflicts))


def _versions(git: _Git, new: str) -> dict[str, set[str]]:
    """path -> every content (blob) the file had in the last LOOK_BACK commits of the branch; "" stands for
    "was not there"."""
    seen: dict[str, set[str]] = {}
    words = git("log", "-m", "--raw", "--no-abbrev", "--no-renames", "-z", "--format=", f"--max-count={LOOK_BACK}", new).split("\0")
    for meta, path in zip(words, words[1:]):
        if meta.lstrip("\n").startswith(":"):
            _, _, before, after, _ = meta.lstrip("\n").split(" ")
            seen.setdefault(path, set()).update("" if set(blob) == {"0"} else blob for blob in (before, after))
    return seen


def _closest(git: _Git, blob: str, versions: set[str]) -> str | None:
    """Of the versions a file had, the one with the fewest changed lines to `blob`; None when no text version is left."""
    best, fewest = None, None
    for version in sorted(versions - {""}):
        counts = git("diff", "--numstat", version, blob).split("\t")[:2]
        if len(counts) == 2 and counts[0].isdigit() and counts[1].isdigit():
            lines = int(counts[0]) + int(counts[1])
            if fewest is None or lines < fewest:
                best, fewest = version, lines
    return best


def _starting_point(git: _Git, folder: _Folder, new: str, scratch: str) -> tuple[str, str, str, int]:
    """For a folder that was copied from no version on record: (the folder as a tree, a commit that holds for
    each file the version the folder's file started from, the newest version as a commit on top of that one,
    how many of HunterAI's files in the folder match no version)."""
    versions, newest = _versions(git, new), folder.entries(new)
    stands = folder.take(sorted(set(newest) | set(versions)))
    have, began, own = folder.entries(stands), {}, 0
    for path in sorted(set(newest) | set(have)):
        mine, theirs = have.get(path), newest.get(path)
        known = versions.get(path, set()) | ({theirs[1]} if theirs else {""})
        if mine == theirs or (mine[1] if mine else "") in known:
            if mine:                                    # a copy of some version, or not received yet: nothing of the owner's
                began[path] = mine
        else:
            own += 1
            start = _closest(git, mine[1], known) if mine else (theirs[1] if theirs else None)
            if start:
                began[path] = ((theirs or mine)[0], start)
    root_ = git("commit-tree", folder.tree(began, os.path.join(scratch, "began")), "-m", "what each file started from", env=NOBODY).strip()
    tip = git("commit-tree", f"{new}^{{tree}}", "-p", root_, "-m", "the newest version", env=NOBODY).strip()
    return stands, root_, tip, own


def update(root: Path, source: str | None = None, branch: str | None = None, *, home: Path | None = None,
           reanchor: bool = False, aside: bool = False, check: bool = False, before_change=lambda: None) -> Outcome:
    """Bring the folder at `root` to the newest `branch` of `source`. `before_change` is called once, after
    the update is known to fit and before the first file changes. Raises Refused when it cannot start."""
    root = Path(root)
    git = _checkout(root)
    branch = branch or BRANCH
    if source is None:
        source = git("config", "--get", "remote.origin.url", ok=(0, 1)).strip() or SOURCE
    try:
        git("fetch", "--quiet", "--no-tags", source, f"+refs/heads/{branch}:{NEW}")
    except Refused as e:
        raise Refused(f"Could not fetch {branch} from {source}: {str(e).splitlines()[-1]}") from None
    head, new = git("rev-parse", "HEAD").strip(), git("rev-parse", NEW).strip()
    if head == new:
        return Outcome("current", old=new, new=new, was=head)
    with tempfile.TemporaryDirectory(prefix="hunterai-update-") as scratch:
        folder = _Folder(git, os.path.join(scratch, "index"))
        own = int(git("rev-list", "--count", f"{new}..{head}").strip())
        if git.true("merge-base", "--is-ancestor", head, new):
            base = head
        elif git.true("merge-base", "--is-ancestor", new, head):
            return Outcome("ahead", old=new, new=new, own_commits=own, was=head)
        elif not reanchor:
            return Outcome("diverged", new=new, own_commits=own, was=head)
        else:
            base = ""
        if base:
            tip, paths = new, sorted(set(git.files(base)) | set(git.files(new)))
            stands, began = folder.take(paths), base
        else:
            stands, began, tip, apart = _starting_point(git, folder, new, scratch)
            if apart > FAR:
                return Outcome("foreign", new=new, own_commits=own, was=head, apart=apart)
            paths = git.files(stands)
        result, conflicts = _merge(git, stands, began, tip)
        set_aside: list[str] = []
        if conflicts and aside:
            set_aside = conflicts
            result, conflicts = _merge(git, folder.without(set_aside, began), began, tip)
            folder.take(paths)                                                 # the index is the folder again
        mine, theirs = set(git.differing(began, stands)), set(git.differing(began, new))
        out = Outcome("conflicts" if conflicts else "updated", old=base, new=new, own_commits=own, was=head, conflicts=conflicts,
                      commits=[s for s in git("log", "--format=%s", f"{base}..{new}").splitlines() if s] if base else [],
                      merged=sorted((mine & theirs & set(git.differing(stands, new))) - set(conflicts) - set(set_aside)),
                      aside=set_aside)
        if conflicts:
            return out
        out.kept, out.brought = git.differing(new, result), git.differing(stands, result)
        if check:
            return out
        before_change()
        changing = set(out.brought)
        for path in sorted((mine & changing) | set(set_aside)):               # the owner's versions, before they change
            if (root / path).is_file():
                if out.saved is None:
                    out.saved = Path(home or os.environ.get("HUNTERAI_HOME") or Path.home() / ".hunterai") / "updates" / time.strftime("%Y%m%d-%H%M%S")
                (out.saved / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / path, out.saved / path)
        for path in set_aside:
            if (root / path).is_file():
                shutil.copy2(root / path, root / (path + ".mine"))
        git("read-tree", "-m", "-u", stands, result, env=folder.env)
        git("reset", "-q", "--mixed", new)
        out.applied = True
        return out


# ---- HunterAI's own processes

def processes(listing: str, own: int) -> list[Proc]:
    """The server and the ingest worker among the lines of `ps -ax -o pid=,args=`: a Python that runs the
    module hunterai or hunterai.worker. Not `uv run ...`, which only waits for one of them, and not this update."""
    found = []
    for line in listing.splitlines():
        words = line.split()
        if len(words) < 4 or not words[0].isdigit() or int(words[0]) == own:
            continue
        if not os.path.basename(words[1]).startswith("python") or "-m" not in words[2:]:
            continue
        module = words[words.index("-m", 2) + 1] if words.index("-m", 2) + 1 < len(words) else ""
        if module in ("hunterai", "hunterai.worker"):
            found.append(Proc(int(words[0]), "worker" if module.endswith("worker") else "server"))
    return sorted(found, key=lambda p: p.pid)


def running(root: Path) -> list[Proc]:
    """HunterAI's processes that run from this folder. Where the system does not say from which folder a
    process runs, every HunterAI process counts."""
    try:
        listing = subprocess.run(["ps", "-ax", "-o", "pid=,args="], capture_output=True, text=True).stdout
    except OSError:
        return []
    here = str(Path(root).resolve())
    mine = []
    for proc in processes(listing, os.getpid()):
        try:
            if not os.readlink(f"/proc/{proc.pid}/cwd").startswith(here):
                continue
        except OSError:
            pass
        mine.append(proc)
    return mine


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(procs: list[Proc], *, signal=os.kill, alive=None, wait: float = 0.2, patience: float = 20.0) -> None:
    """Ask the server to stop as Ctrl+C does and wait for it; the start script then stops its worker. A worker
    that is still there afterwards is asked too. What does not stop when asked is ended."""
    alive = alive or _alive

    def gone(pids: list[int]) -> bool:
        until = time.monotonic() + patience
        while time.monotonic() < until:
            if not any(alive(p) for p in pids):
                return True
            time.sleep(wait)
        return not any(alive(p) for p in pids)

    for kind, steps in (("server", (signals.SIGINT, signals.SIGTERM, signals.SIGKILL)), ("worker", (signals.SIGTERM, signals.SIGKILL))):
        pids = [p.pid for p in procs if p.kind == kind]
        for sig in steps:
            pids = [p for p in pids if alive(p)]
            if not pids:
                break
            for pid in pids:
                try:
                    signal(pid, sig)
                except (ProcessLookupError, PermissionError):      # gone meanwhile, or another user's: the caller sees what is left
                    pass
            if gone(pids):
                break


# ---- the command

def _short(commit: str) -> str:
    return commit[:7]


def _listed(names: list[str], most: int = 12) -> str:
    return ", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")


def _tell(out: Outcome, branch: str, say) -> None:
    if out.status == "current":
        say(f"HunterAI is up to date: {_short(out.new)} is the newest on {branch}.")
    elif out.status == "ahead":
        say(f"This folder is already newer than {branch}: it has {out.own_commits} commit(s) that are not there yet. Nothing to do.")
    elif out.status == "diverged":
        say(f"This folder has {out.own_commits} commit(s) of its own that are not on {branch}. Nothing was changed.\n"
            "If they only mirror what is on GitHub (an update done by hand), run\n"
            "    scripts/update.sh --reanchor\n"
            "It puts the folder on GitHub's history and keeps every difference of yours as an uncommitted change.\n"
            "If they are work of your own, merge with git instead.")
    elif out.status == "foreign":
        say(f"This folder differs from every version on {branch} in {out.apart} files or more, so it is not a copy of that "
            "branch. Nothing was changed.\n"
            "If it holds code that is not merged there yet, name the branch that has it: scripts/update.sh --branch NAME --reanchor")
    elif out.status == "conflicts":
        say("The update was not applied. These files were changed here and in the update at the same place:\n"
            + "".join(f"    {name}\n" for name in out.conflicts)
            + "Nothing was changed. Settle them yourself, or run\n"
            "    scripts/update.sh --aside\n"
            "to take the new version of those files and keep yours beside them as <file>.mine.")
    else:
        if not out.brought:
            say(f"The files already are {_short(out.new)}, the newest on {branch}.")
        elif not out.old:               # the folder was no single version of the branch
            say(f"{'Brought HunterAI' if out.applied else 'An update would bring HunterAI'} to {_short(out.new)}, the newest on "
                f"{branch}: {len(out.brought)} file(s) {'changed' if out.applied else 'would change'}.")
        else:
            say(f"{'Updated HunterAI' if out.applied else 'An update would bring HunterAI'} from {_short(out.old)} to "
                f"{_short(out.new)}: {len(out.commits)} change(s) from {branch}.")
            for subject in out.commits[:15]:
                say(f"  - {subject}")
            if len(out.commits) > 15:
                say(f"  ... and {len(out.commits) - 15} more")
        if out.own_commits and out.applied:
            say(f"The folder was on {out.own_commits} commit(s) of its own (last: {_short(out.was)}); it now stands on {branch}'s "
                "history. `git reflog` still has them.")
        if out.merged:
            say(f"Merged with your own changes: {_listed(out.merged)}.")
        if out.aside:
            say(f"Your version of these could not be merged and is beside the new one as <file>.mine: {_listed(out.aside)}.")
        if out.kept:
            say(f"Your own changes are still in: {_listed(out.kept)}.")
        if out.saved:
            say(f"Your versions from before the update are in {out.saved}.")


def main(argv: list[str] | None = None, *, root: Path | None = None, home: Path | None = None, out=print) -> int:
    parser = argparse.ArgumentParser(prog="scripts/update.sh", description="Bring this folder to the newest HunterAI and start it again.")
    parser.add_argument("--check", action="store_true", help="say what an update would do, and change nothing")
    parser.add_argument("--no-start", action="store_true", help="do not start HunterAI afterwards")
    parser.add_argument("--aside", action="store_true",
                        help="where a file of yours cannot be merged with its new version, take the new one and keep yours as <file>.mine")
    parser.add_argument("--reanchor", action="store_true",
                        help="for a folder with commits of its own that mirror GitHub: put it on GitHub's history, keep your differences")
    parser.add_argument("--branch", default=os.environ.get("HUNTERAI_UPDATE_BRANCH") or BRANCH, help=f"the branch to follow (default {BRANCH})")
    parser.add_argument("--from", dest="source", default=os.environ.get("HUNTERAI_UPDATE_FROM"),
                        help="where to fetch from (default: this folder's origin, else GitHub)")
    args = parser.parse_args(argv)
    root = Path(root or Path(__file__).resolve().parents[2])

    def halt() -> None:
        procs = running(root)
        if procs:
            out(f"Stopping HunterAI (process {', '.join(str(p.pid) for p in procs)}) ...")
            stop(procs)
            left = [p.pid for p in procs if _alive(p.pid)]
            if left:
                raise Refused(f"HunterAI is still running (process {', '.join(map(str, left))}) and its files are not changed "
                              "under it. Stop it, then run the update again. Nothing was changed.")

    try:
        result = update(root, args.source, args.branch, home=home, reanchor=args.reanchor, aside=args.aside,
                        check=args.check, before_change=halt)
    except Refused as e:
        out(str(e))
        return 2
    _tell(result, args.branch, out)
    if result.applied:
        if not args.no_start:
            out("Starting HunterAI ...")
            return START
        out("Start HunterAI with scripts/start.sh.")
    return 0 if result.status in ("current", "updated", "ahead") else 1


if __name__ == "__main__":
    sys.exit(main())
