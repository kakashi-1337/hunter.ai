import pytest

from hunterai import config
from hunterai.kb import limits, sources
from tests.kb_samples import age_staging, collect, folder_payload, reader, tree, zip_bytes


async def test_folder_walk_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 1000); root = tmp_path / "src"
    tree(root, {"a.md": b"# a", "sub/b.py": b"x = 1", ".git/config": b"g", "node_modules/x.js": b"n", "sub/__pycache__/c.pyc": b"c",
                ".env": b"KEY=1", "id_rsa": b"k", "big.txt": b"b" * 2000, "z.zip": zip_bytes({"inner.txt": b"in"})})
    (root / "link").symlink_to(root / "a.md")
    found, problems = await collect(folder_payload(root))
    assert {f.uri for f in found} == {f"{root}/a.md", f"{root}/sub/b.py", f"{root}/z.zip!/inner.txt"} and len(found) == 3
    assert {(q.uri, q.reason) for q in problems} == {
        (f"{root}/.git/", "ignored directory"), (f"{root}/node_modules/", "ignored directory"), (f"{root}/sub/__pycache__/", "ignored directory"),
        (f"{root}/link", "symlink not followed"), (f"{root}/.env", "secret-like file name (not ingested)"),
        (f"{root}/id_rsa", "secret-like file name (not ingested)"), (f"{root}/big.txt", "file too large (over 1000 bytes)")}
    assert all(q.status == "skipped" for q in problems)


async def test_folder_scan_never_follows_a_directory_link_out(tmp_path):
    root, outside = tmp_path / "src", tmp_path / "outside"
    tree(root, {"a.txt": b"a"}); tree(outside, {"secret.txt": b"s"})
    (root / "escape").symlink_to(outside, target_is_directory=True)
    found, problems = await collect(folder_payload(root))
    assert [f.uri for f in found] == [f"{root}/a.txt"] and [(q.uri, q.reason) for q in problems] == [(f"{root}/escape", "symlink not followed")]


async def test_single_file_source(tmp_path):
    tree(tmp_path, {"one.md": b"# one", ".env": b"K=1"})
    found, problems = await collect(folder_payload(tmp_path / "one.md"))
    assert [f.uri for f in found] == [str(tmp_path / "one.md")] and problems == []
    found, problems = await collect(folder_payload(tmp_path / ".env"))
    assert found == [] and [q.reason for q in problems] == ["secret-like file name (not ingested)"]


async def test_stage_file_keeps_relative_paths_and_rejects_unsafe_ones():
    sid = sources.new_staging("col1")
    ok = await sources.stage_file(sid, "docs/a/b.md", reader(b"hi")); bad = await sources.stage_file(sid, "../../x.txt", reader(b"no"))
    assert (sources.staging_path(sid) / "incoming/docs/a/b.md").read_bytes() == b"hi" and ok.rejected is None
    assert bad.rejected == "unsafe path" and not list(config.home().rglob("x.txt"))
    await sources.reject(sid, "big/video.bin", "file too large (over 52428800 bytes)")
    info = sources.staging_info(sid); assert (info.collection_id, info.files, info.bytes, info.job_id) == ("col1", 1, 2, None)
    found, problems = await collect({"collection_id": "col1", "staging_id": sid, "source": {"kind": "upload"}})
    assert [f.uri for f in found] == ["upload:docs/a/b.md"] and [(q.uri, q.status, q.reason) for q in problems] == [
        ("upload:../../x.txt", "skipped", "unsafe path"), ("upload:big/video.bin", "skipped", "file too large (over 52428800 bytes)")]


async def test_stage_file_enforces_caps(monkeypatch):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 10); sid = sources.new_staging("c")
    big = await sources.stage_file(sid, "big.txt", reader(b"x" * 50))
    assert big.rejected == "file too large (over 10 bytes)" and not (sources.staging_path(sid) / "incoming/big.txt").exists()
    monkeypatch.setattr(limits, "MAX_JOB_FILES", 1)
    assert (await sources.stage_file(sid, "a.txt", reader(b"a"))).rejected is None
    assert (await sources.stage_file(sid, "b.txt", reader(b"b"))).rejected == "job file cap reached (1 files)"
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 50 * 1024**2); monkeypatch.setattr(limits, "MAX_JOB_FILES", 50_000)
    monkeypatch.setattr(limits, "MAX_JOB_BYTES", 100); sid = sources.new_staging("c")
    assert (await sources.stage_file(sid, "one.bin", reader(b"1" * 80))).rejected is None
    assert (await sources.stage_file(sid, "two.bin", reader(b"2" * 80))).rejected == "job size cap reached (100 bytes)"
    assert not (sources.staging_path(sid) / "incoming/two.bin").exists() and sources.staging_info(sid).bytes == 80


def test_new_staging_respects_the_disk_floor(monkeypatch):
    monkeypatch.setattr(limits, "free_bytes", lambda: limits.FREE_SPACE_FLOOR - 1)
    with pytest.raises(limits.NoSpace):
        sources.new_staging("c")


@pytest.mark.parametrize("bad", ["..", "a/b", "", "x" * 32, "A" * 32, "abc"])
def test_staging_path_rejects_anything_but_an_id(bad):
    with pytest.raises(ValueError):
        sources.staging_path(bad)


def test_sweep_staging():
    kept, old_done, new_done, fresh, stale = (sources.new_staging("c") for _ in range(5))
    for sid in (kept, old_done, new_done):
        sources.mark_committed(sid, "job")
    age_staging(kept, 120); age_staging(old_done, 120); age_staging(stale, 2 * 86400)
    assert sources.sweep_staging({kept}) == 2
    assert {d.name for d in config.staging_dir().iterdir()} == {kept, new_done, fresh}


# ---- review fixes

async def test_archive_members_follow_the_secret_and_ignored_directory_rules(tmp_path):
    root = tmp_path / "src"
    tree(root, {"repo.zip": zip_bytes({"app/main.py": b"print(1)", ".env": b"KEY=1", "conf/ID_RSA": b"k", "sub/.ENV": b"K=2",
                                       ".git/config": b"[remote] url=https://token@x", "node_modules/a/x.js": b"1", "node_modules/b/y.js": b"2",
                                       ".aws/credentials": b"secret", "cert/site.P12": b"p"})})
    found, problems = await collect(folder_payload(root))
    assert [f.uri for f in found] == [f"{root}/repo.zip!/app/main.py"]
    assert {(q.uri.removeprefix(f"{root}/repo.zip!/"), q.reason) for q in problems} == {
        (".env", "secret-like file name (not ingested)"), ("conf/ID_RSA", "secret-like file name (not ingested)"),
        ("sub/.ENV", "secret-like file name (not ingested)"), (".aws/credentials", "secret-like file name (not ingested)"),
        ("cert/site.P12", "secret-like file name (not ingested)"),
        (".git/", "ignored directory"), ("node_modules/", "ignored directory")}
    left = {p.name for p in config.staging_dir().rglob("*") if p.is_file()} - {"meta.json"}
    assert left == {"main.py"}                                   # nothing secret stays on disk in staging


async def test_a_scan_of_a_parent_folder_never_enters_hunterais_own_data(tmp_path, monkeypatch):
    home = tmp_path / "data" / ".hunterai"
    monkeypatch.setenv("HUNTERAI_HOME", str(home))
    tree(tmp_path / "data", {"notes.txt": b"notes", ".hunterai/server.log": b"log line with a token"})
    found, problems = await collect(folder_payload(tmp_path / "data"))
    assert [f.uri for f in found] == [f"{tmp_path}/data/notes.txt"]
    assert (f"{tmp_path}/data/.hunterai/", "HunterAI's own data folder (not ingested)") in {(q.uri, q.reason) for q in problems}
    monkeypatch.setenv("HUNTERAI_HOME", str(tmp_path / "home"))  # the fixture checks the home it set


async def test_stage_file_path_collisions_are_rejections_not_errors():
    sid = sources.new_staging("c")
    assert (await sources.stage_file(sid, "a", reader(b"1"))).rejected is None
    assert (await sources.stage_file(sid, "a/b", reader(b"2"))).rejected == "path collides with another uploaded file"
    assert (await sources.stage_file(sid, "d/e", reader(b"3"))).rejected is None
    assert (await sources.stage_file(sid, "d", reader(b"4"))).rejected == "path collides with another uploaded file"
    assert sources.staging_info(sid).files == 2
