"""Whether the code on disk is still the code the server runs: after an update or an edit, the
running process goes on with what it loaded, and the owner has to be told to restart it."""
import os

from hunterai import stale, thermal
from hunterai.api import system
from tests.test_api import client  # noqa: F401 (client is a fixture)


def code(root, **files):
    """Write files under `root`; `api__app__py` is api/app.py."""
    for name, text in files.items():
        *folders, stem, suffix = name.split("__")
        path = root.joinpath(*folders, f"{stem}.{suffix}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def watching(root, **files) -> stale.Watch:
    return stale.Watch(code(root, **files), every=0)          # every look reads the files


def test_code_that_did_not_change_needs_no_restart(tmp_path):
    watch = watching(tmp_path, engine__py="x = 1\n", api__app__py="y = 2\n")
    assert watch.changed() is False


def test_a_changed_file_means_a_restart_is_needed(tmp_path):
    watch = watching(tmp_path, engine__py="x = 1\n", api__app__py="y = 2\n")
    (tmp_path / "api" / "app.py").write_text("y = 3\n")
    assert watch.changed() is True


def test_a_change_is_seen_even_when_the_file_keeps_its_time_and_its_length(tmp_path):
    """rsync -t and tar give a file the time it had; a one-character fix keeps its length."""
    watch = watching(tmp_path, engine__py="x = 1\n")
    path = tmp_path / "engine.py"
    before = path.stat()
    path.write_text("x = 2\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert watch.changed() is True


def test_a_file_saved_again_with_what_it_held_needs_no_restart(tmp_path):
    watch = watching(tmp_path, engine__py="x = 1\n")
    path = tmp_path / "engine.py"
    path.write_text("x = 1\n")                    # git checkout, an editor's save: a new time, the same content
    os.utime(path, (path.stat().st_mtime + 60, path.stat().st_mtime + 60))
    assert watch.changed() is False


def test_a_new_file_and_a_removed_file_each_mean_a_restart(tmp_path):
    watch = watching(tmp_path, engine__py="x = 1\n", hop__py="z = 0\n")
    code(tmp_path, tools__skill_tool__py="def tools(): ...\n")
    assert watch.changed() is True
    (tmp_path / "tools" / "skill_tool.py").unlink()
    assert watch.changed() is False               # undone: the code is what the process started with
    (tmp_path / "hop.py").unlink()
    assert watch.changed() is True


def test_a_renamed_file_is_a_change(tmp_path):
    watch = watching(tmp_path, hop__py="one\n", web__py="two\n")
    (tmp_path / "hop.py").rename(tmp_path / "hops.py")        # the same content, in the same order
    assert watch.changed() is True


def test_a_line_that_moves_into_the_next_file_is_a_change(tmp_path):
    watch = watching(tmp_path, a__py="one\ntwo\n", b__py="three\n")
    code(tmp_path, a__py="one\n", b__py="two\nthree\n")
    assert watch.changed() is True


def test_the_schema_counts_and_what_python_writes_for_itself_does_not(tmp_path):
    watch = watching(tmp_path, engine__py="x = 1\n", schema__sql="create table a(b);\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "engine.cpython-312.pyc").write_bytes(b"compiled")
    code(tmp_path, notes__txt="mine")
    (tmp_path / ".engine.py.swp").write_bytes(b"an editor's own file")
    assert watch.changed() is False
    (tmp_path / "schema.sql").write_text("create table a(b, c);\n")
    assert watch.changed() is True


def test_asked_often_the_files_are_read_once_in_a_while(tmp_path, monkeypatch):
    """The page asks every few seconds, and several pages can be open."""
    clock = [100.0]
    monkeypatch.setattr(stale.time, "monotonic", lambda: clock[0])
    watch = stale.Watch(code(tmp_path, engine__py="x = 1\n"), every=5)
    reads = []
    whole = stale.Watch._digest
    monkeypatch.setattr(stale.Watch, "_digest", lambda self: reads.append(1) or whole(self))
    (tmp_path / "engine.py").write_text("x = 22\n")
    assert watch.changed() is False and reads == []            # looked a moment ago, when it was made
    clock[0] += 5
    assert [watch.changed() for _ in range(4)] == [True] * 4 and reads == [1]
    clock[0] += 5
    assert watch.changed() is True and reads == [1, 1]


def test_files_that_cannot_be_read_keep_the_last_answer(tmp_path, monkeypatch):
    watch = watching(tmp_path, engine__py="x = 1\n")

    def gone(self):
        raise OSError("a file went away while it was read")
    monkeypatch.setattr(stale.Watch, "_digest", gone)
    assert watch.changed() is False


def test_what_is_watched_is_the_servers_own_code():
    assert stale._own.root == stale.ROOT
    assert (stale.ROOT / "engine.py").is_file() and (stale.ROOT / "api" / "app.py").is_file()


async def test_the_status_tells_the_page_when_a_restart_is_needed(client, monkeypatch, tmp_path):
    async def nothing(url):
        return None
    monkeypatch.setattr(system, "_fetch", nothing)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: None)
    monkeypatch.setattr(stale, "_own", watching(tmp_path, engine__py="x = 1\n"))
    assert (await client.get("/api/system/status")).json()["restart_needed"] is False
    (tmp_path / "engine.py").write_text("x = 2\n")
    assert (await client.get("/api/system/status")).json()["restart_needed"] is True
