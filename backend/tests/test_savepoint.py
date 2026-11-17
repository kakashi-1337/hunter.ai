"""A save point of the work folder is taken before a run in coding mode changes anything, in a git folder of
HunterAI's own; what the run changed can be listed and put back, file by file, without touching what the owner
changed since. Real git, real folders."""
import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hunterai import config, savepoint


def write(folder: Path, name: str, content: str | bytes) -> Path:
    p = folder / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode())
    return p


@pytest.fixture
def work(tmp_path, isolated_home) -> Path:
    folder = tmp_path / "project"
    write(folder, "app.py", "def main():\n    return 1\n")
    write(folder, "README.md", "# Project\n")
    write(folder, "src/util.py", "X = 1\n")
    return folder


def names(found) -> dict:
    return {f["path"]: f["change"] for f in found}


async def test_what_a_run_changed_is_listed_by_kind(work):
    before = await savepoint.take(work)
    write(work, "app.py", "def main():\n    return 2\n")
    write(work, "new.py", "print('hi')\n")
    (work / "README.md").unlink()
    after = await savepoint.take(work)
    assert names(await savepoint.changed(work, before, after)) == {"app.py": "changed", "new.py": "new", "README.md": "deleted"}


async def test_a_folder_that_did_not_change_has_nothing_to_list(work):
    before = await savepoint.take(work)
    after = await savepoint.take(work)
    assert await savepoint.changed(work, before, after) == []


async def test_undo_puts_every_file_back_as_it_was(work):
    before = await savepoint.take(work)
    write(work, "app.py", "def main():\n    return 2\n")
    write(work, "new/deep/file.py", "print('hi')\n")
    (work / "src" / "util.py").unlink()
    after = await savepoint.take(work)
    done = await savepoint.undo(work, before, after)
    assert sorted(done["restored"]) == ["app.py", "new/deep/file.py", "src/util.py"] and done["left"] == []
    assert (work / "app.py").read_text() == "def main():\n    return 1\n"
    assert not (work / "new" / "deep" / "file.py").exists()
    assert (work / "src" / "util.py").read_text() == "X = 1\n"


async def test_undo_leaves_what_the_owner_changed_after_the_run(work):
    """The run changed two files; the owner then went on working in one of them. Only the other is put back."""
    before = await savepoint.take(work)
    write(work, "app.py", "def main():\n    return 2\n")
    write(work, "README.md", "# Project\n\nrewritten by the run\n")
    after = await savepoint.take(work)
    write(work, "app.py", "def main():\n    return 3  # the owner's own fix\n")
    done = await savepoint.undo(work, before, after)
    assert done == {"restored": ["README.md"], "left": ["app.py"]}
    assert (work / "app.py").read_text() == "def main():\n    return 3  # the owner's own fix\n"
    assert (work / "README.md").read_text() == "# Project\n"


async def test_undo_has_nothing_to_do_for_a_file_that_is_already_as_it_was(work):
    before = await savepoint.take(work)
    write(work, "new.py", "x\n")
    write(work, "app.py", "changed\n")
    after = await savepoint.take(work)
    (work / "new.py").unlink()                                          # the owner removed it himself
    write(work, "app.py", "def main():\n    return 1\n")                # and put this one back by hand
    assert await savepoint.undo(work, before, after) == {"restored": [], "left": []}


async def test_the_owners_own_changes_from_after_the_run_survive_an_undo_of_other_files(work):
    before = await savepoint.take(work)
    write(work, "app.py", "changed by the run\n")
    after = await savepoint.take(work)
    write(work, "notes.txt", "the owner's notes, made after the run\n")
    await savepoint.undo(work, before, after)
    assert (work / "notes.txt").read_text() == "the owner's notes, made after the run\n"


async def test_bytes_that_are_no_text_and_names_with_spaces_come_back_exactly(work):
    blob = bytes(range(256)) * 40
    write(work, "data/blob file.bin", blob)
    write(work, "ünïcode näme.txt", "ä\n")
    before = await savepoint.take(work)
    write(work, "data/blob file.bin", b"\x00\x01")
    (work / "ünïcode näme.txt").unlink()
    after = await savepoint.take(work)
    assert names(await savepoint.changed(work, before, after)) == {"data/blob file.bin": "changed", "ünïcode näme.txt": "deleted"}
    await savepoint.undo(work, before, after)
    assert (work / "data" / "blob file.bin").read_bytes() == blob and (work / "ünïcode näme.txt").read_text() == "ä\n"


async def test_a_program_that_may_be_run_may_still_be_run_after_undo(work):
    script = write(work, "run.sh", "#!/bin/sh\necho 1\n")
    script.chmod(0o755)
    before = await savepoint.take(work)
    script.unlink()
    after = await savepoint.take(work)
    await savepoint.undo(work, before, after)
    assert os.access(work / "run.sh", os.X_OK)


def git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, check=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"}).stdout


async def test_the_owners_own_git_repository_is_never_touched(work):
    """The save point lives in a git folder of HunterAI's own: no ref, no stash, no index entry in the owner's."""
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "first")
    write(work, "app.py", "staged by the owner\n")
    git(work, "add", "app.py")
    # What is staged is compared entry by entry: git itself rewrites the times it keeps in the index file whenever it is asked.
    state = lambda: (git(work, "for-each-ref"), git(work, "stash", "list"), git(work, "status", "--porcelain"),      # noqa: E731
                     git(work, "ls-files", "--stage"), git(work, "rev-parse", "HEAD"), sorted(p.name for p in (work / ".git").iterdir()))
    was = state()
    before = await savepoint.take(work)
    write(work, "src/util.py", "X = 2\n")
    after = await savepoint.take(work)
    assert names(await savepoint.changed(work, before, after)) == {"src/util.py": "changed"}        # nothing of .git is listed
    await savepoint.undo(work, before, after)
    assert state() == was
    assert not list((work / ".git").glob("**/hunterai*"))


async def test_what_the_folder_ignores_is_neither_kept_nor_put_back(work):
    """Keys in a .env file and what a build leaves behind are not copied anywhere."""
    write(work, ".gitignore", ".env\nbuild/\n")
    write(work, ".env", "API_KEY=first\n")
    before = await savepoint.take(work)
    write(work, ".env", "API_KEY=second\n")
    write(work, "build/out.bin", b"x" * 1000)
    after = await savepoint.take(work)
    assert await savepoint.changed(work, before, after) == []
    await savepoint.undo(work, before, after)
    assert (work / ".env").read_text() == "API_KEY=second\n" and (work / "build" / "out.bin").exists()
    own = savepoint.shadow(work)
    assert own.is_dir() and config.home() in own.parents
    kept = subprocess.run(["git", "--git-dir", str(own), "ls-tree", "-r", "--name-only", after], capture_output=True, text=True).stdout
    assert ".gitignore" in kept.split() and ".env" not in kept and "out.bin" not in kept


async def test_the_heavy_folders_of_a_project_without_ignore_rules_are_left_out(work):
    for heavy in ("node_modules/pkg/index.js", ".venv/lib/x.py", "__pycache__/app.pyc", "target/debug/app"):
        write(work, heavy, "generated\n")
    before = await savepoint.take(work)
    write(work, "node_modules/pkg/index.js", "changed by an install\n")
    write(work, "app.py", "changed\n")
    after = await savepoint.take(work)
    assert names(await savepoint.changed(work, before, after)) == {"app.py": "changed"}


@pytest.mark.parametrize("where", ["/", "~", "missing"])
async def test_a_folder_that_is_too_broad_or_not_there_gets_no_save_point(work, where):
    folder = work / "nope" if where == "missing" else Path(where).expanduser()
    assert await savepoint.take(folder) is None


async def test_without_git_there_is_no_save_point_and_no_failure(work, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert await savepoint.take(work) is None


async def test_a_folder_with_too_many_files_gets_no_save_point(work, monkeypatch):
    monkeypatch.setattr(savepoint, "MAX_FILES", 5)
    for n in range(8):
        write(work, f"many/{n}.txt", "x\n")
    assert await savepoint.take(work) is None


async def test_two_folders_have_two_save_points_that_do_not_mix(work, tmp_path):
    other = tmp_path / "other"
    write(other, "app.py", "other project\n")
    a, b = await savepoint.take(work), await savepoint.take(other)
    write(other, "app.py", "changed\n")
    assert await savepoint.changed(work, a, await savepoint.take(work)) == []
    assert names(await savepoint.changed(other, b, await savepoint.take(other))) == {"app.py": "changed"}


# ---- repositories inside the folder, odd names, kinds that change

def a_repository(folder: Path, commit=True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q")
    if commit:
        git(folder, "add", "-A")
        git(folder, "commit", "-q", "-m", "first")


async def test_a_repository_inside_the_folder_is_kept_file_by_file(work):
    """A project cloned into the work folder: git alone would keep a pointer to it and none of its files. Its files
    are kept like any others, its own ignore rules hold, and its own git folder is left as it is."""
    inner = work / "vendor" / "lib"
    write(inner, "lib.py", "V = 1\n")
    write(inner, ".gitignore", "secret.env\n")
    write(inner, "secret.env", "KEY=1\n")
    a_repository(inner)
    was = (git(inner, "ls-files", "--stage"), git(inner, "rev-parse", "HEAD"), git(inner, "for-each-ref"))
    before = await savepoint.take(work)
    write(inner, "lib.py", "V = 2\n")
    write(inner, "secret.env", "KEY=2\n")
    after = await savepoint.take(work)
    assert names(await savepoint.changed(work, before, after)) == {"vendor/lib/lib.py": "changed"}
    assert await savepoint.undo(work, before, after) == {"restored": ["vendor/lib/lib.py"], "left": []}
    assert (inner / "lib.py").read_text() == "V = 1\n" and (inner / "secret.env").read_text() == "KEY=2\n"
    assert (git(inner, "ls-files", "--stage"), git(inner, "rev-parse", "HEAD"), git(inner, "for-each-ref")) == was


async def test_a_repository_without_a_commit_inside_the_folder_does_not_cost_the_save_point(work):
    a_repository(work / "fresh", commit=False)
    before = await savepoint.take(work)
    assert before is not None
    write(work, "fresh/first.py", "x = 1\n")
    assert names(await savepoint.changed(work, before, await savepoint.take(work))) == {"fresh/first.py": "new"}


async def test_a_repository_inside_a_repository_inside_the_folder_is_kept_too(work):
    outer, inner = work / "outer", work / "outer" / "inner"
    write(outer, "o.py", "O = 1\n")
    a_repository(outer)
    write(inner, "i.py", "I = 1\n")
    a_repository(inner)
    before = await savepoint.take(work)
    write(inner, "i.py", "I = 2\n")
    after = await savepoint.take(work)
    assert names(await savepoint.changed(work, before, after)) == {"outer/inner/i.py": "changed"}


async def test_a_repository_the_run_itself_made_is_listed_file_by_file(work):
    before = await savepoint.take(work)
    write(work, "cloned/main.go", "package main\n")
    a_repository(work / "cloned")
    assert names(await savepoint.changed(work, before, await savepoint.take(work))) == {"cloned/main.go": "new"}


ODD = ("-rf", ":(top)odd.txt", "*.py", "a b/[x].txt", "--output=x")


async def test_names_that_look_like_options_or_patterns_are_taken_as_names(work):
    for name in ODD:
        write(work, name, "first\n")
    before = await savepoint.take(work)
    for name in ODD:
        write(work, name, "second\n")
    after = await savepoint.take(work)
    write(work, "app.py", "the owner's, from after the run\n")          # "*.py" is one file's name here, not every Python file
    assert (await savepoint.undo(work, before, after))["restored"] == sorted(ODD)
    assert all((work / name).read_text() == "first\n" for name in ODD)
    assert (work / "app.py").read_text() == "the owner's, from after the run\n"


async def test_a_file_the_run_turned_into_a_folder_comes_back(work):
    before = await savepoint.take(work)
    (work / "README.md").unlink()
    write(work, "README.md/part.md", "split up by the run\n")
    after = await savepoint.take(work)
    assert await savepoint.undo(work, before, after) == {"restored": ["README.md", "README.md/part.md"], "left": []}
    assert (work / "README.md").read_text() == "# Project\n"


async def test_a_folder_the_owner_put_work_into_is_not_replaced_by_the_file_it_was(work):
    before = await savepoint.take(work)
    (work / "README.md").unlink()
    write(work, "README.md/part.md", "split up by the run\n")
    after = await savepoint.take(work)
    write(work, "README.md/mine.md", "the owner's, made after the run\n")
    assert await savepoint.undo(work, before, after) == {"restored": ["README.md/part.md"], "left": ["README.md"]}
    assert (work / "README.md" / "mine.md").read_text() == "the owner's, made after the run\n"


async def test_a_file_the_owner_changed_is_not_removed_to_bring_back_the_folder_it_replaced(work):
    before = await savepoint.take(work)
    shutil.rmtree(work / "src")
    write(work, "src", "a file now, by the run\n")
    after = await savepoint.take(work)
    write(work, "src", "the owner's words\n")
    assert await savepoint.undo(work, before, after) == {"restored": [], "left": ["src", "src/util.py"]}
    assert (work / "src").read_text() == "the owner's words\n"


async def test_a_link_comes_back_as_a_link(work):
    (work / "latest").symlink_to("app.py")
    before = await savepoint.take(work)
    (work / "latest").unlink()
    write(work, "latest", "a file now\n")
    after = await savepoint.take(work)
    await savepoint.undo(work, before, after)
    assert (work / "latest").is_symlink() and os.readlink(work / "latest") == "app.py"
    assert (work / "app.py").read_text() == "def main():\n    return 1\n"          # nothing was written through the link


async def test_many_files_are_put_back_in_one_go(work):
    before = await savepoint.take(work)
    for n in range(400):
        write(work, f"gen/{n:03}/file with a long name to fill the command line {n:03}.txt", "made by the run\n")
    write(work, "app.py", "changed\n")
    after = await savepoint.take(work)
    done = await savepoint.undo(work, before, after)
    assert len(done["restored"]) == 401 and not (work / "gen").exists() and (work / "app.py").read_text() == "def main():\n    return 1\n"


async def test_a_long_list_of_files_is_put_back_in_several_goes(work, monkeypatch):
    """What git is handed in one go has a size the system limits; no feature of a newer git is needed for more."""
    for n in range(120):
        write(work, f"src/mod_{n:03}.py", f"V = {n}\n")
    before = await savepoint.take(work)
    for n in range(120):
        write(work, f"src/mod_{n:03}.py", "changed by the run\n")
    after = await savepoint.take(work)
    asked, real = [], savepoint._git

    async def counting(workdir, *args, **more):
        asked.append(args)
        return await real(workdir, *args, **more)
    monkeypatch.setattr(savepoint, "_git", counting)
    monkeypatch.setattr(savepoint, "ARGS_BYTES", 500)
    done = await savepoint.undo(work, before, after)
    assert len(done["restored"]) == 120 and all((work / f"src/mod_{n:03}.py").read_text() == f"V = {n}\n" for n in range(120))
    goes = [a for a in asked if a[0] == "checkout"]
    assert len(goes) > 3 and all(sum(len(x) + 1 for x in a) < 700 for a in goes) and not any(x.startswith("--pathspec") for a in goes for x in a)


async def test_an_undo_that_broke_off_can_be_gone_on_with(work, monkeypatch):
    before = await savepoint.take(work)
    write(work, "app.py", "changed\n")
    write(work, "new.py", "made by the run\n")
    after = await savepoint.take(work)
    real = savepoint._git

    async def breaking(workdir, *args, **more):
        if args[0] == "checkout":
            raise savepoint.SavePointError("git checkout failed in the folder: error: unable to create file app.py: No space left on device")
        return await real(workdir, *args, **more)
    monkeypatch.setattr(savepoint, "_git", breaking)
    with pytest.raises(savepoint.SavePointError) as failed:
        await savepoint.undo(work, before, after)
    assert str(failed.value) == ("git checkout failed in the folder: error: unable to create file app.py: No space left on device. "
                                 "Some of the files may be back already: Undo again goes on with the rest.")
    monkeypatch.setattr(savepoint, "_git", real)
    assert await savepoint.undo(work, before, after) == {"restored": ["app.py"], "left": []}        # new.py went the first time
    assert (work / "app.py").read_text() == "def main():\n    return 1\n" and not (work / "new.py").exists()


async def test_a_file_whose_name_is_no_text_is_left_and_named_rather_than_guessed_at(work):
    try:
        (work / os.fsdecode(b"caf\xe9.txt")).write_text("first\n")
    except OSError:
        pytest.skip("this file system takes no such name")
    before = await savepoint.take(work)
    (work / os.fsdecode(b"caf\xe9.txt")).write_text("second\n")
    write(work, "app.py", "changed\n")
    after = await savepoint.take(work)
    done = await savepoint.undo(work, before, after)
    assert done["restored"] == ["app.py"] and len(done["left"]) == 1 and done["left"][0].startswith("caf")


# ---- the store

async def test_a_save_point_that_is_no_longer_kept_says_so_and_changes_nothing(work):
    before = await savepoint.take(work)
    write(work, "app.py", "changed\n")
    after = await savepoint.take(work)
    shutil.rmtree(savepoint.shadow(work))
    with pytest.raises(savepoint.SavePointError, match="no longer kept"):
        await savepoint.undo(work, before, after)
    assert (work / "app.py").read_text() == "changed\n"


async def test_a_store_that_was_cleared_and_filled_again_does_not_hold_the_earlier_save_points(work):
    before = await savepoint.take(work)
    write(work, "app.py", "changed\n")
    after = await savepoint.take(work)
    shutil.rmtree(savepoint.shadow(work))
    assert await savepoint.take(work) == after                          # a later run fills the store again, with the folder as it is now
    with pytest.raises(savepoint.SavePointError, match="no longer kept"):
        await savepoint.undo(work, before, after)
    assert (work / "app.py").read_text() == "changed\n"


@pytest.mark.parametrize("bad", ["--output=/tmp/x", "HEAD", "", "abc", "g" * 40])
async def test_what_is_not_the_id_of_a_save_point_never_reaches_git(work, bad):
    good = await savepoint.take(work)
    with pytest.raises(savepoint.SavePointError, match="no longer kept"):
        await savepoint.undo(work, bad, good)
    with pytest.raises(savepoint.SavePointError, match="no longer kept"):
        await savepoint.changed(work, good, bad)


async def test_a_folder_that_did_not_change_costs_no_new_space(work):
    """Two save points of the same content are one and the same: a run that changes nothing leaves nothing behind."""
    first = await savepoint.take(work)
    kept = sorted(p.name for p in savepoint.shadow(work).rglob("*") if p.is_file() and "objects" in p.parts)
    await asyncio.sleep(1.1)                                            # git's own clock counts in whole seconds
    assert await savepoint.take(work) == first
    assert sorted(p.name for p in savepoint.shadow(work).rglob("*") if p.is_file() and "objects" in p.parts) == kept


async def test_the_copies_are_readable_by_the_owner_alone(work):
    (work / "app.py").chmod(0o600)
    await savepoint.take(work)
    assert savepoint.shadow(work).parent.stat().st_mode & 0o777 == 0o700


async def test_a_lock_left_by_a_git_that_was_killed_does_not_end_save_points_for_good(work):
    assert await savepoint.take(work) is not None
    lock = savepoint.shadow(work) / "index.lock"
    lock.write_bytes(b"")
    write(work, "app.py", "changed\n")
    assert await savepoint.take(work) is None                       # a lock this fresh may belong to a git at work
    long_ago = lock.stat().st_mtime - savepoint.MAX_SECONDS - 60
    os.utime(lock, (long_ago, long_ago))
    assert await savepoint.take(work) is not None


def a_git_that_hangs(tmp_path, monkeypatch) -> Path:
    """A `git` that waits until it is asked to end, and then says so in a file: the real one removes its lock then."""
    folder, mark = tmp_path / "bin", tmp_path / "asked"
    folder.mkdir()
    (folder / "git").write_text(f"#!/bin/sh\ntrap 'echo asked > \"{mark}\"; exit 1' TERM\nsleep 30 &\nwait\n")
    (folder / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")
    return mark


async def test_a_git_that_takes_too_long_is_asked_to_end_before_it_is_killed(work, tmp_path, monkeypatch):
    mark = a_git_that_hangs(tmp_path, monkeypatch)
    monkeypatch.setattr(savepoint, "MAX_SECONDS", 0.3)
    with pytest.raises(savepoint.SavePointError, match="took longer than 0.3 seconds"):
        await savepoint._git(work, "add", "-A", ".")
    assert mark.read_text() == "asked\n"


async def test_a_git_of_a_run_that_is_stopped_is_asked_to_end_too(work, tmp_path, monkeypatch):
    mark = a_git_that_hangs(tmp_path, monkeypatch)
    waiting = asyncio.create_task(savepoint._git(work, "add", "-A", "."))
    await asyncio.sleep(0.3)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert mark.read_text() == "asked\n"


async def test_a_fault_while_the_save_point_is_taken_does_not_stop_the_call_it_stands_before(work, monkeypatch):
    async def broken(folder):
        raise RuntimeError("no home folder could be found")
    monkeypatch.setattr(savepoint, "_take", broken)
    await savepoint.before_run("r1", work)
    got = await savepoint.after_run("r1")
    assert got["files"] == [] and got["reason"] == ("No save point was kept for this run: RuntimeError: no home folder could be found. "
                                                    "What it changed cannot be listed or put back from here.")


async def test_a_folder_that_holds_too_much_gets_no_save_point(work, monkeypatch):
    monkeypatch.setattr(savepoint, "MAX_BYTES", 1000)
    write(work, "big.bin", b"x" * 2000)
    assert await savepoint.take(work) is None


async def test_what_is_ignored_does_not_count_towards_too_much(work, monkeypatch):
    monkeypatch.setattr(savepoint, "MAX_BYTES", 1000)
    monkeypatch.setattr(savepoint, "MAX_FILES", 6)
    write(work, ".gitignore", "data/\n")
    write(work, "data/big.bin", b"x" * 5000)
    for n in range(20):
        write(work, f"data/{n}.txt", "x\n")
    assert await savepoint.take(work) is not None


async def test_a_folder_that_holds_hunterais_own_data_gets_no_save_point(work, isolated_home):
    assert await savepoint.take(config.home()) is None and await savepoint.take(config.home().parent) is None
    assert await savepoint.take(savepoint.shadow(work).parent) is None


# ---- the save point of a run, and why there is none

async def of_a_run(folder, run="r1") -> dict:
    await savepoint.before_run(run, folder)
    return await savepoint.after_run(run)


async def test_a_run_with_a_save_point_gets_what_it_changed(work):
    await savepoint.before_run("r1", work)
    write(work, "app.py", "changed\n")
    await savepoint.before_run("r1", work)                              # a later call of the same run: the first save point stands
    got = await savepoint.after_run("r1")
    assert got["files"] == [{"path": "app.py", "change": "changed"}] and got["reason"] == "" and got["folder"] == str(work)
    assert got["before"] and got["after"] and got["before"] != got["after"]
    assert await savepoint.after_run("r1") is None and await savepoint.after_run("never") is None


async def test_a_run_stopped_while_its_changes_are_listed_still_gets_its_list(work, monkeypatch):
    """Stop arrives while the folder is read at the end of the run: the list is made when the run is closed."""
    await savepoint.before_run("r1", work)
    write(work, "app.py", "changed\n")
    real, reading = savepoint._take, asyncio.Event()

    async def slow(folder):
        reading.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(savepoint, "_take", slow)
    listing = asyncio.create_task(savepoint.after_run("r1"))
    await reading.wait()
    listing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await listing
    monkeypatch.setattr(savepoint, "_take", real)
    got = await savepoint.after_run("r1")
    assert got["files"] == [{"path": "app.py", "change": "changed"}] and await savepoint.after_run("r1") is None


async def test_the_folder_is_read_once_for_a_run_not_before_each_of_its_calls(work, monkeypatch):
    taken, real = [], savepoint._take

    async def counting(folder):
        taken.append(folder)
        return await real(folder)
    monkeypatch.setattr(savepoint, "_take", counting)
    for _ in range(5):
        await savepoint.before_run("r1", work)
    assert len(taken) == 1
    await savepoint.after_run("r1")
    assert len(taken) == 2


@pytest.mark.parametrize("case, says", [
    ("files", "holds more than 4 files that are not ignored"), ("bytes", "holds more than 1,000 bytes in files that are not ignored"),
    ("git", "git is not installed on this machine"), ("home", "your home folder, a folder above it, or one that holds HunterAI's own data"),
    ("missing", "is not there")])
async def test_a_run_without_a_save_point_is_told_why(work, monkeypatch, case, says):
    folder = work
    if case == "files":
        monkeypatch.setattr(savepoint, "MAX_FILES", 4)
        for n in range(6):
            write(work, f"many/{n}.txt", "x\n")
    elif case == "bytes":
        monkeypatch.setattr(savepoint, "MAX_BYTES", 1000)
        write(work, "big.bin", b"x" * 2000)
    elif case == "git":
        monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    elif case == "home":
        folder = Path.home()
    else:
        folder = work / "nope"
    got = await of_a_run(folder)
    assert (got["files"], got["before"], got["after"]) == ([], "", "")
    assert got["reason"].startswith("No save point was kept for this run: ") and says in got["reason"]
    assert got["reason"].endswith(". What it changed cannot be listed or put back from here.")


async def test_a_run_whose_folder_cannot_be_read_at_its_end_is_told_so(work, monkeypatch):
    await savepoint.before_run("r1", work)
    monkeypatch.setattr(savepoint, "MAX_FILES", 4)
    for n in range(8):
        write(work, f"made/{n}.txt", "by the run\n")
    got = await savepoint.after_run("r1")
    assert got["files"] == [] and got["before"] and got["after"] == ""
    assert got["reason"] == ("What this run changed could not be listed at its end: the work folder holds more than 4 files that "
                             "are not ignored. Its changes cannot be put back from here.")


# ---- a run in coding mode

from hunterai import db, engine  # noqa: E402
from hunterai.providers.base import TextDelta, ToolCall  # noqa: E402
from hunterai.repo import chats, messages, runs, settings  # noqa: E402
from hunterai.tools import native  # noqa: E402
from hunterai.types import TextBlock  # noqa: E402
from tests.test_api import client  # noqa: E402,F401 (client is a fixture)
from tests.test_engine import go, no_bytes, use  # noqa: E402
from tests.toolkit import Turns  # noqa: E402


async def coding_chat(conn, work: Path, mode="coding", text="fix the bug"):
    """A chat whose work folder is `work`, where writing and running commands is allowed without a question."""
    native.register_all()
    await settings.put(conn, "permissions", {"risk": {"write": "allow", "execute": "allow"}})
    chat = await chats.create(conn, None, mode, "abliteration", "m", {})
    chat = await chats.update(conn, chat.id, workdir=str(work))
    return chat, await messages.add(conn, chat.id, None, "user", [TextBlock(text=text)])


def block(msg, kind="changes"):
    return next((b for b in msg.blocks if b.type == kind), None)


async def test_a_run_in_coding_mode_lists_what_it_changed_under_its_answer(conn, work):
    use(abliteration=Turns("abliteration",
                           [ToolCall("c1", "edit", {"path": "app.py", "old_string": "return 1", "new_string": "return 2"}),
                            ToolCall("c2", "write", {"path": "tests/test_app.py", "content": "def test_main():\n    assert True\n"})],
                           [TextDelta("Fixed.")]))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    assert run.status == "done"
    found = block(msg)
    assert found.files == [{"path": "app.py", "change": "changed"}, {"path": "tests/test_app.py", "change": "new"}]
    assert (found.run_id, found.folder, found.undone, found.left) == (run.id, str(work), False, [])
    assert found.before and found.after and found.before != found.after
    assert [b.type for b in msg.blocks][-2:] == ["text", "changes"]


async def test_what_a_command_changed_is_listed_like_what_the_file_tools_changed(conn, work):
    use(abliteration=Turns("abliteration", [ToolCall("c1", "execute", {"command": "rm README.md && echo 'Y = 2' > src/extra.py"})],
                           [TextDelta("Done.")]))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    assert block(msg).files == [{"path": "README.md", "change": "deleted"}, {"path": "src/extra.py", "change": "new"}]


async def test_a_run_that_only_reads_takes_no_save_point_and_lists_nothing(conn, work):
    use(abliteration=Turns("abliteration", [ToolCall("c1", "read", {"path": "app.py"})], [TextDelta("It returns 1.")]))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    assert block(msg) is None and not savepoint.shadow(work).exists()


async def test_a_run_whose_commands_change_nothing_lists_nothing(conn, work):
    use(abliteration=Turns("abliteration", [ToolCall("c1", "execute", {"command": "ls"})], [TextDelta("Listed.")]))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    assert run.status == "done" and block(msg) is None


async def test_other_modes_keep_no_save_point(conn, work):
    use(abliteration=Turns("abliteration", [ToolCall("c1", "write", {"path": "notes.txt", "content": "x\n"})], [TextDelta("Noted.")]))
    chat, user = await coding_chat(conn, work, mode="pentesting")
    run, msg = await go(conn, chat, user)
    assert (work / "notes.txt").exists() and block(msg) is None


async def test_a_run_that_fails_after_changing_files_still_lists_them(conn, work):
    from hunterai.providers.base import ProviderError
    use(abliteration=Turns("abliteration", [ToolCall("c1", "write", {"path": "half.py", "content": "x = 1\n"})],
                           ProviderError("unavailable", "the provider went away", 503), ProviderError("unavailable", "still away", 503)))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    assert run.status == "error" and block(msg).files == [{"path": "half.py", "change": "new"}]


async def test_the_list_of_changes_is_the_owners_and_never_goes_to_a_model(conn, work):
    p = Turns("abliteration", [ToolCall("c1", "write", {"path": "a.py", "content": "x = 1\n"})], [TextDelta("Wrote it.")], [TextDelta("Yes.")])
    use(abliteration=p)
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    again = await messages.add(conn, chat.id, msg.id, "user", [TextBlock(text="sure?")])
    await go(conn, chat, again)
    assert "changes" not in [b.type for m in p.calls[-1]["messages"] for b in m.blocks]


async def test_a_run_whose_folder_has_no_save_point_says_so_under_its_answer(client, conn, work, monkeypatch):
    monkeypatch.setattr(savepoint, "MAX_FILES", 2)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "write", {"path": "a.py", "content": "x = 1\n"})], [TextDelta("Wrote it.")]))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    found = block(msg)
    assert (found.files, found.before, found.after) == ([], "", "")
    assert found.reason == ("No save point was kept for this run: the work folder holds more than 2 files that are not ignored. "
                            "What it changed cannot be listed or put back from here.")
    r = await client.post(f"/api/runs/{run.id}/undo")
    assert (r.status_code, r.json()["detail"]) == (404, "This run changed no file in a folder with a save point, so there is nothing to put back.")


async def test_a_run_that_only_reads_in_such_a_folder_says_nothing(conn, work, monkeypatch):
    monkeypatch.setattr(savepoint, "MAX_FILES", 2)
    use(abliteration=Turns("abliteration", [ToolCall("c1", "read", {"path": "app.py"})], [TextDelta("It returns 1.")]))
    chat, user = await coding_chat(conn, work)
    run, msg = await go(conn, chat, user)
    assert block(msg) is None


async def test_a_call_that_was_denied_takes_no_save_point(conn, work):
    native.register_all()
    await settings.put(conn, "permissions", {"risk": {"write": "deny"}})
    use(abliteration=Turns("abliteration", [ToolCall("c1", "write", {"path": "a.py", "content": "x = 1\n"})], [TextDelta("I may not.")]))
    chat = await chats.create(conn, None, "coding", "abliteration", "m", {})
    chat = await chats.update(conn, chat.id, workdir=str(work))
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="write it")])
    run, msg = await go(conn, chat, user)
    assert block(msg) is None and not savepoint.shadow(work).exists() and not (work / "a.py").exists()


async def test_stopping_a_run_while_its_changes_are_listed_keeps_the_answer_and_the_list(conn, work, monkeypatch):
    use(abliteration=Turns("abliteration", [ToolCall("c1", "edit", {"path": "app.py", "old_string": "return 1", "new_string": "return 2"})],
                           [TextDelta("Fixed.")]))
    chat, user = await coding_chat(conn, work)
    real, readings, reading = savepoint._take, [], asyncio.Event()

    async def slow_at_the_end(folder):
        readings.append(folder)
        if len(readings) == 2:                                          # the reading at the end of the run
            reading.set()
            await asyncio.sleep(30)
        return await real(folder)
    monkeypatch.setattr(savepoint, "_take", slow_at_the_end)
    run_id, mid = await engine.start_run(db.connect, chat.id, user.id, no_bytes)
    await asyncio.wait_for(reading.wait(), 20)
    await engine.cancel(run_id)
    await asyncio.wait_for(engine.wait(run_id), 20)
    run, msg = await runs.get(conn, run_id), await messages.get(conn, mid)
    assert run.status == "done" and [b.text for b in msg.blocks if b.type == "text"] == ["Fixed."]
    assert block(msg).files == [{"path": "app.py", "change": "changed"}]


# ---- undo

async def a_run_that_changed_two_files(conn, work):
    use(abliteration=Turns("abliteration",
                           [ToolCall("c1", "edit", {"path": "app.py", "old_string": "return 1", "new_string": "return 2"}),
                            ToolCall("c2", "write", {"path": "new.py", "content": "print('hi')\n"})], [TextDelta("Fixed.")]))
    chat, user = await coding_chat(conn, work)
    return await go(conn, chat, user)


async def test_undo_puts_the_work_folder_back_and_marks_the_list_as_undone(client, conn, work):
    run, msg = await a_run_that_changed_two_files(conn, work)
    r = await client.post(f"/api/runs/{run.id}/undo")
    assert r.status_code == 200, r.text
    got = r.json()
    assert (got["restored"], got["left"]) == (["app.py", "new.py"], [])
    assert (work / "app.py").read_text() == "def main():\n    return 1\n" and not (work / "new.py").exists()
    shown = next(b for b in got["message"]["blocks"] if b["type"] == "changes")
    assert (shown["undone"], shown["left"]) == (True, [])
    assert block(await messages.get(conn, msg.id)).undone is True               # and it is still so after a reload


async def test_undo_says_which_files_it_left_because_the_owner_changed_them_since(client, conn, work):
    run, msg = await a_run_that_changed_two_files(conn, work)
    write(work, "app.py", "def main():\n    return 3\n")
    got = (await client.post(f"/api/runs/{run.id}/undo")).json()
    assert (got["restored"], got["left"]) == (["new.py"], ["app.py"])
    kept = block(await messages.get(conn, msg.id))
    assert (kept.left, kept.undone) == (["app.py"], False)                      # not all of it is undone: it can be asked again
    assert (work / "app.py").read_text() == "def main():\n    return 3\n"


async def test_an_undo_that_left_files_can_be_asked_again_once_they_are_as_the_run_left_them(client, conn, work):
    run, msg = await a_run_that_changed_two_files(conn, work)
    write(work, "app.py", "def main():\n    return 3\n")
    await client.post(f"/api/runs/{run.id}/undo")
    again = await client.post(f"/api/runs/{run.id}/undo")
    assert (again.status_code, again.json()["restored"], again.json()["left"]) == (200, [], ["app.py"])
    write(work, "app.py", "def main():\n    return 2\n")                       # the owner takes his own change out again
    last = (await client.post(f"/api/runs/{run.id}/undo")).json()
    assert (last["restored"], last["left"]) == (["app.py"], [])
    kept = block(await messages.get(conn, msg.id))
    assert (kept.left, kept.undone) == ([], True) and (work / "app.py").read_text() == "def main():\n    return 1\n"
    assert (await client.post(f"/api/runs/{run.id}/undo")).status_code == 409


async def test_a_run_can_be_undone_once(client, conn, work):
    run, _ = await a_run_that_changed_two_files(conn, work)
    assert (await client.post(f"/api/runs/{run.id}/undo")).status_code == 200
    again = await client.post(f"/api/runs/{run.id}/undo")
    assert (again.status_code, again.json()["detail"]) == (409, "The changes of this run were already put back.")


async def test_a_run_without_a_save_point_cannot_be_undone(client, conn, work):
    use(abliteration=Turns("abliteration", [TextDelta("Nothing to do.")]))
    chat, user = await coding_chat(conn, work)
    run, _ = await go(conn, chat, user)
    r = await client.post(f"/api/runs/{run.id}/undo")
    assert (r.status_code, r.json()["detail"]) == (404, "This run changed no file in a folder with a save point, so there is nothing to put back.")
    assert (await client.post("/api/runs/nope/undo")).status_code == 404


async def test_undo_waits_while_another_run_of_the_chat_is_changing_files(client, conn, work):
    run, msg = await a_run_that_changed_two_files(conn, work)
    chat = await chats.get(conn, run.chat_id)
    going = await runs.create(conn, chat.id, None, None)                         # a run of the same chat that has not ended
    r = await client.post(f"/api/runs/{run.id}/undo")
    assert (r.status_code, r.json()["detail"]) == (409, "A run of this chat is still going: wait for it to end, or stop it, then undo.")
    assert (work / "new.py").exists()
    await runs.finish(conn, going.id, "done", None)
    assert (await client.post(f"/api/runs/{run.id}/undo")).status_code == 200


async def test_undo_of_a_folder_that_is_gone_says_so(client, conn, work):
    run, _ = await a_run_that_changed_two_files(conn, work)
    shutil.rmtree(work)
    r = await client.post(f"/api/runs/{run.id}/undo")
    assert r.status_code == 409 and str(work) in r.json()["detail"] and "nothing was put back" in r.json()["detail"]
