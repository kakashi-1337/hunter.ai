import bz2
import gzip
import lzma
import shutil
import subprocess
import tarfile

import pytest

from hunterai import config
from hunterai.kb import archives, limits, parsers
from tests.kb_samples import (
    GROW_7Z,
    HANG,
    NO_RAR_7Z,
    assert_dead,
    collect,
    folder_payload,
    put,
    rar4_bytes,
    rar5_bytes,
    tar_bytes,
    zip_bytes,
)

try:
    from compression import zstd  # Python 3.14 and later
except ImportError:
    from backports import zstd

needs_7z = pytest.mark.skipif(shutil.which("7z") is None, reason="7z not installed")

LISTING = """Path = d
Size = 0
Packed Size = 0
Modified = 2026-11-14 00:05:08.1618766
Attributes = D drwxrwxr-x
CRC = 
Encrypted = -
Method = 
Block = 

Path = a.txt
Size = 6
Packed Size = 23
Modified = 2026-11-14 00:05:08.1618766
Attributes = A -rw-rw-r--
CRC = 363A3020
Encrypted = -
Method = LZMA2:12
Block = 0

Path = link
Size = 11
Packed Size = 
Modified = 2026-11-14 00:05:08.1622439
Attributes = A lrwxrwxrwx
CRC = 291FB90A
Encrypted = -
Method = LZMA2:12
Block = 0

"""   # `7z l -slt -ba` from 7-Zip 26.00 (x64), archive made with `7z a -snl`


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out"


def files(root):
    return [f for f in root.rglob("*") if f.is_file()] if root.exists() else []


def written(tmp_path, *given):
    """Every file and every link under the test's folder but the ones the test put there itself."""
    return sorted(str(f.relative_to(tmp_path)) for f in tmp_path.rglob("*")
                  if (f.is_file() or f.is_symlink()) and f not in {*given, tmp_path / "test.env"})


async def test_zip_with_nested_tar_gz(tmp_path, out):
    inner = tar_bytes([(tarfile.TarInfo("y/z.md"), b"# z")], mode="w:gz")
    p = put(tmp_path, "a.zip", zip_bytes({"x.txt": b"x", "inner.tar.gz": inner}))
    found, problems = await archives.unpack(p, "a.zip", out, limits.Budget())
    assert sorted(f.uri for f in found) == ["a.zip!/inner.tar.gz!/y/z.md", "a.zip!/x.txt"] and problems == []
    assert all(f.path.is_file() and f.path.resolve().is_relative_to(out.resolve()) for f in found)
    assert not list(out.rglob("inner.tar.gz"))


async def test_traversal_and_absolute_entries_are_recorded_and_never_written(tmp_path, out):          # Review Focus 2
    p = put(tmp_path / "in", "h.zip", zip_bytes({"../evil.txt": b"x", "/abs.txt": b"y", "ok.txt": b"fine"}))
    t = put(tmp_path / "in", "h.tar", tar_bytes([(tarfile.TarInfo("../../evil2.txt"), b"z")]))
    found, problems = await archives.unpack(p, "h.zip", out, limits.Budget())
    found2, problems2 = await archives.unpack(t, "h.tar", out, limits.Budget())
    assert [f.uri for f in found] == ["h.zip!/ok.txt"] and found2 == []
    assert [(q.status, q.reason) for q in problems + problems2] == [("skipped", "unsafe path in archive")] * 3
    assert "h.zip!/../evil.txt" in {q.uri for q in problems}
    written = {f for f in tmp_path.rglob("*") if f.is_file()} - {p, t, tmp_path / "test.env"}
    assert len(written) == 1 and all(f.resolve().is_relative_to(out.resolve()) for f in written)


async def test_links_and_devices_are_recorded_not_extracted(tmp_path, out):                           # Review Focus 2
    def info(name, type_, link=""):
        ti = tarfile.TarInfo(name); ti.type = type_; ti.linkname = link; return ti
    p = put(tmp_path, "l.tar", tar_bytes([(info("sym", tarfile.SYMTYPE, "/etc/passwd"), None), (info("hard", tarfile.LNKTYPE, "real.txt"), None),
                                         (info("dev", tarfile.CHRTYPE), None), (tarfile.TarInfo("real.txt"), b"real")]))
    found, problems = await archives.unpack(p, "l.tar", out, limits.Budget())
    assert [f.uri for f in found] == ["l.tar!/real.txt"] and not any(f.is_symlink() for f in out.rglob("*"))
    assert {(q.uri, q.reason) for q in problems} == {("l.tar!/sym", "symlink in archive (not extracted)"),
        ("l.tar!/hard", "hardlink in archive (not extracted)"), ("l.tar!/dev", "special file in archive (not extracted)")}


async def test_ratio_cap_stops_a_bomb_and_frees_its_bytes(tmp_path, out, monkeypatch):                # Review Focus 2
    monkeypatch.setattr(limits, "RATIO_FLOOR_BYTES", 1024 * 1024); monkeypatch.setattr(limits, "MAX_RATIO", 10)
    budget = limits.Budget()
    found, problems = await archives.unpack(put(tmp_path, "bomb.zip", zip_bytes({"zeros.txt": b"\0" * (8 * 1024 * 1024)})), "bomb.zip", out, budget)
    assert found == [] and [(q.uri, q.reason) for q in problems] == [("bomb.zip", "compression ratio above 10:1 (possible archive bomb)")]
    assert budget.stored_bytes == 0 and budget.files == 0 and not files(out)


async def test_nesting_depth_cap(tmp_path, out):
    data = zip_bytes({"l4.txt": b"4"})
    for level in (3, 2, 1):
        data = zip_bytes({f"l{level}.txt": str(level).encode(), "next.zip": data})
    found, problems = await archives.unpack(put(tmp_path, "n.zip", data), "n.zip", out, limits.Budget())
    assert sorted(f.path.name for f in found) == ["l1.txt", "l2.txt", "l3.txt"]
    assert [(q.uri, q.reason) for q in problems] == [("n.zip!/next.zip!/next.zip!/next.zip", "archive nested deeper than 3 levels")]


async def test_job_file_cap_stops_the_archive_with_one_record(tmp_path, out, monkeypatch):
    monkeypatch.setattr(limits, "MAX_JOB_FILES", 2)
    p = put(tmp_path, "many.zip", zip_bytes({f"f{i}.txt": b"x" for i in range(5)}))
    found, problems = await archives.unpack(p, "many.zip", out, limits.Budget())
    assert len(found) == 2 and [(q.uri, q.reason) for q in problems] == [("many.zip!/*", "job file cap reached (2 files): 3 entries not extracted")]


async def test_job_byte_cap_stops_the_archive_with_one_record(tmp_path, out, monkeypatch):
    monkeypatch.setattr(limits, "MAX_JOB_BYTES", 3000); budget = limits.Budget()
    p = put(tmp_path, "big.zip", zip_bytes({f"f{i}.bin": bytes([65 + i]) * 2000 for i in range(3)}))
    found, problems = await archives.unpack(p, "big.zip", out, budget)
    assert len(found) == 1 and [(q.uri, q.reason) for q in problems] == [("big.zip!/*", "job size cap reached (3000 bytes): 2 entries not extracted")]
    assert budget.stored_bytes == 2000 and len(files(out)) == 1


async def test_per_file_cap_skips_only_that_member(tmp_path, out, monkeypatch):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 1000)
    p = put(tmp_path, "m.zip", zip_bytes({"big.txt": b"b" * 5000, "small.txt": b"s"}))
    found, problems = await archives.unpack(p, "m.zip", out, limits.Budget())
    assert [f.uri for f in found] == ["m.zip!/small.txt"] and [(q.uri, q.reason) for q in problems] == [("m.zip!/big.txt", "file too large (over 1000 bytes)")]
    assert len(files(out)) == 1


async def test_disk_floor_blocks_unpacking(tmp_path, out, monkeypatch):
    monkeypatch.setattr(limits, "free_bytes", lambda: limits.FREE_SPACE_FLOOR - 1)
    found, problems = await archives.unpack(put(tmp_path, "a.zip", zip_bytes({"a.txt": b"a"})), "a.zip", out, limits.Budget())
    assert found == [] and [q.reason for q in problems] == [f"disk space floor reached ({limits.FREE_SPACE_FLOOR} bytes must stay free)"]


def test_free_bytes_is_real_unless_the_test_flag_is_set(monkeypatch):
    assert limits.free_bytes() == 1 << 40
    monkeypatch.delenv("HUNTERAI_FAKE_EMBED")
    assert abs(limits.free_bytes() - shutil.disk_usage(config.home()).free) < 64 * 1024**2


async def test_gz_xz_tgz_and_tar_xz(tmp_path, out):
    cases = {"notes.txt.gz": (gzip.compress(b"notes"), "notes.txt.gz!/notes.txt", b"notes"),
             "data.sql.xz": (lzma.compress(b"select 1;"), "data.sql.xz!/data.sql", b"select 1;"),
             "t.tgz": (tar_bytes([(tarfile.TarInfo("a.txt"), b"tgz")], "w:gz"), "t.tgz!/a.txt", b"tgz"),
             "t.tar.xz": (tar_bytes([(tarfile.TarInfo("a.txt"), b"txz")], "w:xz"), "t.tar.xz!/a.txt", b"txz")}
    for name, (data, uri, content) in cases.items():
        found, problems = await archives.unpack(put(tmp_path, name, data), name, out, limits.Budget())
        assert [f.uri for f in found] == [uri] and problems == [] and found[0].path.read_bytes() == content


async def test_corrupt_archive_is_a_problem_not_an_exception(tmp_path, out):
    found, problems = await archives.unpack(put(tmp_path, "c.zip", b"PK\x03\x04garbage"), "c.zip", out, limits.Budget())
    assert found == [] and [(q.status, q.reason) for q in problems] == [("skipped", "unreadable archive: BadZipFile")]


@pytest.mark.parametrize("name,ok", [("a/b.txt", "a/b.txt"), ("./a//b.txt", "a/b.txt"), ("a\\b.txt", "a/b.txt"), ("../x", None),
    ("a/../../x", None), ("/etc/passwd", None), ("C:\\x", None), ("", None), ("a\0b", None)])
def test_safe_member(name, ok):
    assert archives.safe_member(name) == ok


def test_parse_7z_listing():
    d, a, link = archives.parse_7z_listing(LISTING)
    assert (d.path, d.is_dir, d.is_link) == ("d", True, False) and (a.path, a.size, a.is_dir, a.is_link, a.encrypted) == ("a.txt", 6, False, False, False)
    assert (link.path, link.is_link) == ("link", True)


def make_7z(tmp_path, name, tree_files, link=None):
    src = tmp_path / "src7"
    for rel, data in tree_files.items():
        put((src / rel).parent, (src / rel).name, data)
    if link:
        (src / link[0]).symlink_to(link[1])
    subprocess.run(["7z", "a", "-snl", str(tmp_path / name), "."], cwd=src, check=True, capture_output=True)
    return tmp_path / name


@pytest.mark.skipif(shutil.which("7z") is None, reason="7z not installed")
async def test_real_7z_roundtrip(tmp_path, out):
    p = make_7z(tmp_path, "r.7z", {"a.txt": b"hello", "d/b.md": b"# b"})
    found, problems = await archives.unpack(p, "r.7z", out, limits.Budget())
    assert [f.uri for f in found] == ["r.7z!/a.txt", "r.7z!/d/b.md"] and problems == [] and found[0].path.read_bytes() == b"hello"


async def test_7z_hang_is_killed_and_reported_failed(fake_bin, tmp_path, out, monkeypatch):          # Review Focus 3
    fd = fake_bin("7z", HANG); monkeypatch.setattr(limits, "LIST_TIMEOUT", 0.3)
    found, problems = await archives.unpack(put(tmp_path, "x.7z", b"7z\xbc\xaf\x27\x1c"), "x.7z", out, limits.Budget())
    assert found == [] and [(q.uri, q.status, q.reason) for q in problems] == [("x.7z", "failed", "7z timed out after 0.3 s")]
    for name in ("main.pid", "child.pid"):
        assert_dead(int((fd / name).read_text()))


async def test_7z_missing_is_recorded(tmp_path, out, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    found, problems = await archives.unpack(put(tmp_path, "x.7z", b"7z"), "x.7z", out, limits.Budget())
    assert [(q.status, q.reason) for q in problems] == [("skipped", "7z is not installed")]


@pytest.mark.skipif(shutil.which("7z") is None, reason="7z not installed")
async def test_7z_with_a_symlink_entry_is_not_extracted(tmp_path, out):                                # Review Focus 2
    p = make_7z(tmp_path, "s.7z", {"ok.txt": b"ok"}, link=("link", "/etc/passwd"))
    found, problems = await archives.unpack(p, "s.7z", out, limits.Budget())
    assert found == [] and {(q.uri, q.status, q.reason) for q in problems} == {
        ("s.7z!/link", "skipped", "symlink in archive (not extracted)"),
        ("s.7z", "skipped", "7z archive contains links or unsafe paths (not extracted)")}
    assert not out.exists() or not [f for f in out.rglob("*") if f.is_file() or f.is_symlink()]


@pytest.mark.parametrize("patch,reason", [({"RATIO_FLOOR_BYTES": 100_000, "MAX_RATIO": 10}, "compression ratio above 10:1 (possible archive bomb)"),
    ({"MAX_JOB_BYTES": 100_000}, "job size cap reached (100000 bytes)")])
async def test_7z_growth_is_capped(fake_bin, tmp_path, out, monkeypatch, patch, reason):             # Review Focus 2
    fd = fake_bin("7z", GROW_7Z)
    for k, v in patch.items():
        monkeypatch.setattr(limits, k, v)
    found, problems = await archives.unpack(put(tmp_path, "g.7z", b"7z\xbc\xaf\x27\x1c"), "g.7z", out, limits.Budget())
    assert found == [] and [(q.uri, q.status, q.reason) for q in problems] == [("g.7z", "skipped", reason)]
    assert not files(out)
    assert_dead(int((fd / "main.pid").read_text()))


# ---- bz2, zst and rar

@pytest.mark.parametrize("name", ["a.tar.bz2", "a.tbz2", "a.tbz", "notes.txt.bz2", "a.tar.zst", "a.tzst", "dump.sql.zst", "a.rar", "A.RAR",
                                  "B.TAR.ZST"])
def test_bz2_zst_and_rar_names_are_archives(name):
    assert parsers.kind_for(name) == "archive"


async def test_bz2_and_the_bzipped_tars(tmp_path, out):
    cases = {"notes.txt.bz2": (bz2.compress(b"notes"), "notes.txt.bz2!/notes.txt", b"notes"),
             "t.tar.bz2": (tar_bytes([(tarfile.TarInfo("d/a.txt"), b"one")], "w:bz2"), "t.tar.bz2!/d/a.txt", b"one"),
             "t.tbz2": (tar_bytes([(tarfile.TarInfo("a.txt"), b"two")], "w:bz2"), "t.tbz2!/a.txt", b"two"),
             "t.tbz": (tar_bytes([(tarfile.TarInfo("a.txt"), b"three")], "w:bz2"), "t.tbz!/a.txt", b"three")}
    for name, (data, uri, content) in cases.items():
        found, problems = await archives.unpack(put(tmp_path, name, data), name, out, limits.Budget())
        assert [f.uri for f in found] == [uri] and problems == [] and found[0].path.read_bytes() == content


async def test_zst_and_the_zstandard_tars(tmp_path, out):
    tar = tar_bytes([(tarfile.TarInfo("d/a.txt"), b"in a tar"), (tarfile.TarInfo("b.md"), b"# b")])
    cases = {"dump.sql.zst": (zstd.compress(b"select 1;"), ["dump.sql.zst!/dump.sql"], b"select 1;"),
             "t.tar.zst": (zstd.compress(tar), ["t.tar.zst!/d/a.txt", "t.tar.zst!/b.md"], b"in a tar"),
             "t.tzst": (zstd.compress(tar), ["t.tzst!/d/a.txt", "t.tzst!/b.md"], b"in a tar")}
    for name, (data, uris, content) in cases.items():
        found, problems = await archives.unpack(put(tmp_path, name, data), name, out, limits.Budget())
        assert [f.uri for f in found] == uris and problems == [] and found[0].path.read_bytes() == content
        assert all(f.path.resolve().is_relative_to(out.resolve()) for f in found)


async def test_an_archive_inside_a_zst_is_unpacked_too(tmp_path, out):
    inner = zip_bytes({"x.txt": b"x"})
    found, problems = await archives.unpack(put(tmp_path, "a.zip.zst", zstd.compress(inner)), "a.zip.zst", out, limits.Budget())
    assert [f.uri for f in found] == ["a.zip.zst!/a.zip!/x.txt"] and problems == [] and not list(out.rglob("a.zip"))


@pytest.mark.parametrize("name,data,why", [
    ("c.bz2", b"BZh9 this is no bzip2 stream", "OSError"),
    ("c.tar.bz2", b"no tar and no bzip2 either", "ReadError"),
    ("c.zst", b"\x28\xb5\x2f\xfd and then nothing a frame could be", "ZstdError"),
    ("cut.zst", zstd.compress(bytes(range(256)) * 40)[:30], "EOFError"),
    ("c.tar.zst", zstd.compress(b"zstandard, but no tar inside" * 40), "ReadError"),
    ("c.tzst", b"neither", "ZstdError")])
async def test_a_broken_bz2_or_zst_is_a_problem_not_an_exception(tmp_path, out, name, data, why):
    budget = limits.Budget()
    found, problems = await archives.unpack(put(tmp_path, name, data), name, out, budget)
    assert found == [] and [(q.uri, q.status, q.reason) for q in problems] == [(name, "skipped", f"unreadable archive: {why}")]
    assert budget.files == 0 and budget.stored_bytes == 0


@pytest.mark.parametrize("name,pack", [("zeros.txt.bz2", bz2.compress), ("zeros.txt.zst", zstd.compress),
                                       ("zeros.tar.zst", lambda data: zstd.compress(tar_bytes([(tarfile.TarInfo("zeros.txt"), data)])))])
async def test_the_ratio_cap_stops_a_bz2_or_zst_bomb_and_frees_its_bytes(tmp_path, out, monkeypatch, name, pack):
    monkeypatch.setattr(limits, "RATIO_FLOOR_BYTES", 1024 * 1024); monkeypatch.setattr(limits, "MAX_RATIO", 10)
    budget = limits.Budget()
    found, problems = await archives.unpack(put(tmp_path, name, pack(b"\0" * (8 * 1024 * 1024))), name, out, budget)
    assert found == [] and [(q.uri, q.reason) for q in problems] == [(name, "compression ratio above 10:1 (possible archive bomb)")]
    assert budget.stored_bytes == 0 and budget.files == 0 and not files(out)


@pytest.mark.parametrize("name,pack", [("e.tar.bz2", bz2.compress), ("e.tar.zst", zstd.compress), ("e.tzst", zstd.compress)])
async def test_a_path_that_leaves_and_a_link_in_a_bzipped_or_zstandard_tar_are_never_written(tmp_path, out, name, pack):
    link = tarfile.TarInfo("ln"); link.type = tarfile.SYMTYPE; link.linkname = "/etc/passwd"
    tar = tar_bytes([(tarfile.TarInfo("../escape.txt"), b"out"), (tarfile.TarInfo("/abs.txt"), b"abs"), (link, None),
                     (tarfile.TarInfo("ok.txt"), b"in")])
    p = put(tmp_path / "in", name, pack(tar))
    found, problems = await archives.unpack(p, name, out, limits.Budget())
    assert [f.uri for f in found] == [f"{name}!/ok.txt"]
    assert sorted((q.uri, q.reason) for q in problems) == [(f"{name}!/../escape.txt", "unsafe path in archive"),
                                                            (f"{name}!//abs.txt", "unsafe path in archive"),
                                                            (f"{name}!/ln", "symlink in archive (not extracted)")]
    assert written(tmp_path, p) == [str(found[0].path.relative_to(tmp_path))] and found[0].path.is_relative_to(out)


async def test_a_folder_scan_unpacks_the_new_archives(tmp_path):
    root = tmp_path / "src"
    put(root, "logs.tar.zst", zstd.compress(tar_bytes([(tarfile.TarInfo("app.log"), b"started")])))
    put(root, "old.tbz2", tar_bytes([(tarfile.TarInfo("notes.md"), b"# old")], "w:bz2"))
    found, problems = await collect(folder_payload(root))
    assert sorted(f.uri.removeprefix(str(root)) for f in found) == ["/logs.tar.zst!/app.log", "/old.tbz2!/notes.md"] and problems == []


@needs_7z
@pytest.mark.parametrize("rar", [rar4_bytes, rar5_bytes])
async def test_a_rar_is_unpacked(tmp_path, out, rar):
    p = put(tmp_path, "r.rar", rar(("notes/readme.md", b"# Notes"), ("a.txt", b"alpha")))
    found, problems = await archives.unpack(p, "r.rar", out, limits.Budget())
    assert [f.uri for f in found] == ["r.rar!/a.txt", "r.rar!/notes/readme.md"] and problems == []
    assert [f.path.read_bytes() for f in found] == [b"alpha", b"# Notes"]


@needs_7z
@pytest.mark.parametrize("rar", [rar4_bytes, rar5_bytes])
async def test_a_rar_with_a_path_that_leaves_is_not_extracted(tmp_path, out, rar):
    p = put(tmp_path / "in", "e.rar", rar(("../escape.txt", b"out"), ("ok.txt", b"in")))
    found, problems = await archives.unpack(p, "e.rar", out, limits.Budget())
    assert found == [] and {(q.uri, q.status, q.reason) for q in problems} == {
        ("e.rar!/../escape.txt", "skipped", "unsafe path in archive"),
        ("e.rar", "skipped", "rar archive contains links or unsafe paths (not extracted)")}
    assert written(tmp_path, p) == []


@needs_7z
async def test_a_rar_with_a_symbolic_link_is_not_extracted(tmp_path, out):
    p = put(tmp_path / "in", "s.rar", rar5_bytes(("ln", "/etc/passwd", "link"), ("ok.txt", b"in")))
    found, problems = await archives.unpack(p, "s.rar", out, limits.Budget())
    assert found == [] and {(q.uri, q.status, q.reason) for q in problems} == {
        ("s.rar!/ln", "skipped", "symlink in archive (not extracted)"),
        ("s.rar", "skipped", "rar archive contains links or unsafe paths (not extracted)")}
    assert written(tmp_path, p) == []


@needs_7z
async def test_a_link_in_an_old_rar_is_never_made_a_link(tmp_path, out):
    """7-Zip does not say that a member of a RAR 4 archive is a link: it writes the target's name into a plain file."""
    p = put(tmp_path / "in", "s4.rar", rar4_bytes(("ln", "/etc/passwd", "link"), ("ok.txt", b"in")))
    found, problems = await archives.unpack(p, "s4.rar", out, limits.Budget())
    assert sorted(f.uri for f in found) == ["s4.rar!/ln", "s4.rar!/ok.txt"] and problems == []
    assert not [f for f in tmp_path.rglob("*") if f.is_symlink()] and len(written(tmp_path, p)) == 2
    assert next(f for f in found if f.uri.endswith("/ln")).path.read_bytes() == b"/etc/passwd"


@pytest.mark.parametrize("name", ["p.rar", "P.RAR"])
async def test_a_rar_that_this_7z_cannot_unpack_says_what_is_missing(fake_bin, tmp_path, out, name):
    fake_bin("7z", NO_RAR_7Z)
    budget = limits.Budget()
    found, problems = await archives.unpack(put(tmp_path / "in", name, b"Rar!\x1a\x07\x00"), name, out, budget)
    assert found == [] and [(q.uri, q.status, q.reason) for q in problems] == [(name, "skipped", archives.NO_RAR_DECODER)]
    assert "7zip-rar" in archives.NO_RAR_DECODER and not files(out) and budget.files == 0


async def test_a_rar_that_fails_for_another_reason_keeps_the_words_of_7z(fake_bin, tmp_path, out, monkeypatch):
    fake_bin("7z", NO_RAR_7Z); monkeypatch.setenv("FAKE_7Z_SAYS", "Data Error")
    found, problems = await archives.unpack(put(tmp_path / "in", "d.rar", b"Rar!\x1a\x07\x00"), "d.rar", out, limits.Budget())
    assert found == [] and [(q.uri, q.status, q.reason) for q in problems] == [("d.rar", "failed", "7z failed: ERROR: Data Error : big.txt\n")]
    assert not files(out)


async def test_a_7z_that_cannot_unpack_a_7z_keeps_its_own_words(fake_bin, tmp_path, out):
    """The hint is about rar. Another archive that fails the same way says what 7z said."""
    fake_bin("7z", NO_RAR_7Z)
    found, problems = await archives.unpack(put(tmp_path / "in", "p.7z", b"7z\xbc\xaf\x27\x1c"), "p.7z", out, limits.Budget())
    assert found == [] and [(q.uri, q.status) for q in problems] == [("p.7z", "failed")]
    assert problems[0].reason.startswith("7z failed: ") and "Unsupported Method" in problems[0].reason


async def test_a_rar_without_7z_is_recorded(tmp_path, out, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    found, problems = await archives.unpack(put(tmp_path, "x.rar", b"Rar!\x1a\x07\x00"), "x.rar", out, limits.Budget())
    assert found == [] and [(q.status, q.reason) for q in problems] == [("skipped", "7z is not installed")]
