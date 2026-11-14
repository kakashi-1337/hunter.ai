"""Unpack archives for ingest without trusting anything in them: no path leaves the
destination, links and devices are never created, sizes are counted as they are written,
and a bad archive is a recorded problem, never an exception."""
from __future__ import annotations

import asyncio
import gzip
import lzma
import os
import re
import shutil
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..db import new_id
from . import limits, parsers, tools
from .limits import Budget

BLOCK = 1024 * 1024


@dataclass
class Found:
    uri: str
    path: Path
    size: int


@dataclass
class Problem:
    uri: str
    status: Literal["skipped", "failed"]
    reason: str


@dataclass
class SevenZipEntry:
    path: str
    size: int
    is_dir: bool
    is_link: bool
    encrypted: bool


def safe_member(name: str) -> str | None:
    """The member's relative path, or None when it must not be written."""
    if not name or "\0" in name:
        return None
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or any(len(p.encode()) > 255 for p in parts):
        return None
    out = "/".join(parts)
    return out if len(out.encode()) <= 1024 else None


def bomb() -> str:
    return f"compression ratio above {limits.MAX_RATIO}:1 (possible archive bomb)"


class _Stop(Exception):
    """A job-level refusal: this archive stops here."""


class _Bomb(Exception):
    pass


class _Writer:
    """Copies members into one archive's own directory, counting real bytes."""

    def __init__(self, archive: Path, out: Path, budget: Budget):
        self.out, self.budget = out, budget
        self.archive_size = max(1, archive.stat().st_size)
        self.written = 0
        self.files: list[tuple[Path, int]] = []

    def copy(self, rel: str, src) -> tuple[Path, int] | str:
        """Write one member. Returns (path, size), or the reason it was skipped.
        Raises _Stop for a job-level refusal and _Bomb when the ratio cap is passed."""
        why = self.budget.refusal(0, stored=True)
        if why:
            raise _Stop(why)
        room, room_why = self.budget.room()
        dest = self.out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with open(dest, "wb") as f:
                while True:
                    block = src.read(BLOCK)
                    if not block:
                        break
                    size += len(block)
                    if size > room:
                        raise _Stop(room_why) if room_why != limits.too_large() else OverflowError
                    if (self.written + size > limits.RATIO_FLOOR_BYTES
                            and self.written + size > limits.MAX_RATIO * self.archive_size):
                        raise _Bomb
                    f.write(block)
        except OverflowError:
            dest.unlink(missing_ok=True)
            return limits.too_large()
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        self.written += size
        self.budget.add(size, stored=True)
        self.files.append((dest, size))
        return dest, size

    def undo(self) -> None:
        for path, size in self.files:
            path.unlink(missing_ok=True)
            self.budget.files -= 1
            self.budget.stored_bytes -= size
        self.files.clear()
        shutil.rmtree(self.out, ignore_errors=True)


def _zip_members(archive: Path):
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        yield len(infos)
        for i in infos:
            mode = i.external_attr >> 16
            if i.is_dir():
                yield i.filename, "dir", None
            elif stat.S_ISLNK(mode):
                yield i.filename, "symlink", None
            elif i.flag_bits & 0x1:
                yield i.filename, "encrypted", None
            else:
                with z.open(i) as src:
                    yield i.filename, "file", src


def _tar_members(archive: Path):
    with tarfile.open(archive, "r:*") as t:
        members = t.getmembers()
        yield len(members)
        for m in members:
            if m.isdir():
                yield m.name, "dir", None
            elif m.issym():
                yield m.name, "symlink", None
            elif m.islnk():
                yield m.name, "hardlink", None
            elif not m.isfile():
                yield m.name, "special", None
            else:
                src = t.extractfile(m)
                try:
                    yield m.name, "file", src
                finally:
                    if src:
                        src.close()


def _single(opener, suffix_len: int):
    def members(archive: Path):
        yield 1
        with opener(archive, "rb") as src:
            yield archive.name[:-suffix_len], "file", src
    return members


_SKIPS = {"symlink": "symlink in archive (not extracted)", "hardlink": "hardlink in archive (not extracted)",
          "special": "special file in archive (not extracted)", "encrypted": "encrypted archive member"}


def _reader(archive: Path):
    name = archive.name.lower()
    if name.endswith(".zip"):
        return _zip_members
    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz")):
        return _tar_members
    if name.endswith(".gz"):
        return _single(gzip.open, 3)
    if name.endswith(".xz"):
        return _single(lzma.open, 3)
    return None


def _extract(archive: Path, uri: str, out: Path, budget: Budget) -> tuple[list[Found], list[Problem]]:
    """Runs in a thread. Everything but .7z."""
    found: list[Found] = []
    problems: list[Problem] = []
    writer = _Writer(archive, out, budget)
    total, seen = 0, 0
    try:
        gen = _reader(archive)(archive)
        total = next(gen)
        for name, kind, src in gen:
            seen += 1
            member_uri = f"{uri}!/{name}"
            rel = safe_member(name)
            if rel is None:
                problems.append(Problem(member_uri, "skipped", "unsafe path in archive"))
            elif kind == "dir":
                continue
            elif kind in _SKIPS:
                problems.append(Problem(member_uri, "skipped", _SKIPS[kind]))
            else:
                got = writer.copy(rel, src)
                if isinstance(got, str):
                    problems.append(Problem(member_uri, "skipped", got))
                else:
                    found.append(Found(member_uri, got[0], got[1]))
    except _Stop as e:
        left = total - seen + 1
        problems.append(Problem(f"{uri}!/*", "skipped", f"{e}: {left} entries not extracted"))
    except _Bomb:
        writer.undo()
        return [], [Problem(uri, "skipped", bomb())]
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, lzma.LZMAError, OSError, RuntimeError,
            NotImplementedError, StopIteration) as e:
        problems.append(Problem(uri, "skipped", f"unreadable archive: {type(e).__name__}"))
    return found, problems


def parse_7z_listing(text: str) -> list[SevenZipEntry]:
    """`7z l -slt -ba` output: blocks of "Key = value" lines separated by blank lines."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        fields = dict(m.groups() for m in re.finditer(r"^([A-Za-z ]+?) = ?(.*)$", block, re.M))
        if "Path" not in fields:
            continue
        attrs = fields.get("Attributes", "")
        mode = attrs.split()[-1] if attrs.split() else ""
        out.append(SevenZipEntry(
            path=fields["Path"], size=int(fields.get("Size") or 0) if (fields.get("Size") or "0").isdigit() else 0,
            is_dir=fields.get("Folder") == "+" or attrs.startswith("D") or mode.startswith("d"),
            is_link=mode.startswith("l") or bool(fields.get("Symbolic Link")) or bool(fields.get("Hard Link")),
            encrypted=fields.get("Encrypted") == "+"))
    return out


async def _unpack_7z(archive: Path, uri: str, out: Path, budget: Budget) -> tuple[list[Found], list[Problem]]:
    problems: list[Problem] = []
    try:
        listing = await tools.run_tool(["7z", "l", "-slt", "-ba", "-sccUTF-8", "--", str(archive)],
                                       timeout=limits.LIST_TIMEOUT, cwd=archive.parent)
    except tools.ToolMissing:
        return [], [Problem(uri, "skipped", "7z is not installed")]
    except tools.ToolTimeout:
        return [], [Problem(uri, "failed", f"7z timed out after {limits.LIST_TIMEOUT:g} s")]
    except tools.ToolLimit:
        return [], [Problem(uri, "skipped", "unreadable archive: listing too large")]
    if listing.returncode != 0:
        return [], [Problem(uri, "failed", "7z failed: " + listing.stderr.decode("utf-8", "replace")[-200:])]
    entries = parse_7z_listing(listing.stdout.decode("utf-8", "replace"))
    wanted: dict[str, int] = {}
    refuse = False
    for e in entries:
        rel = safe_member(e.path)
        member_uri = f"{uri}!/{e.path}"
        if rel is None:
            problems.append(Problem(member_uri, "skipped", "unsafe path in archive")); refuse = True
        elif e.is_link:
            problems.append(Problem(member_uri, "skipped", "symlink in archive (not extracted)")); refuse = True
        elif e.is_dir:
            continue
        elif e.encrypted:
            problems.append(Problem(member_uri, "skipped", "encrypted archive member"))
        else:
            wanted[rel] = e.size
    if refuse:   # 7-Zip extracts an archive as a whole: one hostile entry costs the rest
        return [], [*problems, Problem(uri, "skipped", "7z archive contains links or unsafe paths (not extracted)")]
    if budget.files + len(wanted) > limits.MAX_JOB_FILES:
        return [], [*problems, Problem(uri, "skipped", limits.file_cap())]
    allowed = limits.MAX_JOB_BYTES - budget.stored_bytes
    if sum(wanted.values()) > allowed:
        return [], [*problems, Problem(uri, "skipped", limits.byte_cap())]
    try:
        limits.require_space(sum(wanted.values()))
    except limits.NoSpace as e:
        return [], [*problems, Problem(uri, "skipped", str(e))]
    ratio_bound = max(limits.RATIO_FLOOR_BYTES, limits.MAX_RATIO * archive.stat().st_size)
    out.mkdir(parents=True, exist_ok=True)
    try:
        res = await tools.run_tool(["7z", "x", "-y", "-bd", f"-o{out}", "--", str(archive)],
                                   timeout=limits.EXTRACT_TIMEOUT, cwd=archive.parent, grow_dir=out,
                                   grow_limit=min(allowed, ratio_bound))
    except tools.ToolTimeout:
        shutil.rmtree(out, ignore_errors=True)
        return [], [*problems, Problem(uri, "failed", f"7z timed out after {limits.EXTRACT_TIMEOUT:g} s")]
    except tools.ToolLimit:
        shutil.rmtree(out, ignore_errors=True)
        return [], [*problems, Problem(uri, "skipped", bomb() if ratio_bound <= allowed else limits.byte_cap())]
    except tools.ToolMissing:
        return [], [*problems, Problem(uri, "skipped", "7z is not installed")]
    if res.returncode != 0:
        shutil.rmtree(out, ignore_errors=True)
        return [], [*problems, Problem(uri, "failed", "7z failed: " + res.stderr.decode("utf-8", "replace")[-200:])]
    found: list[Found] = []
    for dirpath, dirnames, names in os.walk(out):
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                os.unlink(os.path.join(dirpath, d)); dirnames.remove(d)
        for n in sorted(names):
            p = Path(dirpath) / n
            st = os.lstat(p)
            rel = p.relative_to(out).as_posix()
            if not stat.S_ISREG(st.st_mode) or rel not in wanted:
                p.unlink(missing_ok=True)
            elif st.st_size > limits.MAX_FILE_BYTES:
                p.unlink(missing_ok=True)
                problems.append(Problem(f"{uri}!/{rel}", "skipped", limits.too_large()))
            else:
                budget.add(st.st_size, stored=True)
                found.append(Found(f"{uri}!/{rel}", p, st.st_size))
    return sorted(found, key=lambda f: f.uri), problems


async def unpack(archive: Path, uri: str, dest: Path, budget: Budget, depth: int = 1) -> tuple[list[Found], list[Problem]]:
    """Unpack one archive into its own fresh directory under `dest`. Never raises for a bad
    archive. Archives inside it are unpacked too, up to limits.MAX_ARCHIVE_DEPTH."""
    try:
        limits.require_space()
    except limits.NoSpace as e:
        return [], [Problem(uri, "skipped", str(e))]
    out = dest / new_id()
    if archive.name.lower().endswith(".7z"):
        found, problems = await _unpack_7z(archive, uri, out, budget)
    elif _reader(archive) is None:
        return [], [Problem(uri, "skipped", "unreadable archive: unknown format")]
    else:
        out.mkdir(parents=True, exist_ok=True)
        found, problems = await asyncio.to_thread(_extract, archive, uri, out, budget)
    result: list[Found] = []
    for f in found:
        if parsers.kind_for(f.path.name) != "archive":
            result.append(f)
            continue
        if depth >= limits.MAX_ARCHIVE_DEPTH:
            problems.append(Problem(f.uri, "skipped", f"archive nested deeper than {limits.MAX_ARCHIVE_DEPTH} levels"))
        else:
            inner_found, inner_problems = await unpack(f.path, f.uri, dest, budget, depth + 1)
            result += inner_found
            problems += inner_problems
        f.path.unlink(missing_ok=True)       # the nested archive itself is not kept
        budget.files -= 1
        budget.stored_bytes -= f.size
    return result, problems
