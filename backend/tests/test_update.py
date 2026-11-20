"""`scripts/update.sh`: bring the folder to the newest HunterAI and keep what the owner changed.
Every test uses real git repositories: one that stands for GitHub, one that is the owner's folder."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from hunterai import update

ME = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **ME}, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout.rstrip("\n")         # the first line of `git status --porcelain` can start with a space


def write(root: Path, name: str, text: str | bytes, executable=False) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    if executable:
        path.chmod(0o755)
    return path


def commit(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


RULES = "".join(f"rule {n}\n" for n in range(1, 21))        # a file long enough for two changes that do not meet


class World:
    """`up` is the repository on GitHub; `folder` is the owner's copy of it."""

    def __init__(self, tmp: Path):
        self.up, self.folder, self.home = tmp / "github", tmp / "hunterai", tmp / "home"
        self.home.mkdir()
        self.up.mkdir()
        git(self.up, "init", "-q", "-b", "main")
        write(self.up, ".gitignore", "/bin/\n*.db\n")
        write(self.up, "backend/hunterai/engine.py", "def run():\n    return 1\n")
        write(self.up, "backend/hunterai/modes.py", RULES)
        write(self.up, "backend/hunterai/old.py", "gone = 'soon'\n")
        write(self.up, "scripts/start.sh", "#!/usr/bin/env bash\necho started\n", executable=True)
        self.first = commit(self.up, "first")
        git(tmp, "clone", "-q", str(self.up), str(self.folder))

    def release(self, message="a change", **files) -> str:
        """A new commit on GitHub. A value of None removes the file."""
        for name, text in files.items():
            if text is None:
                (self.up / name).unlink()
            else:
                write(self.up, name, text)
        return commit(self.up, message)

    def update(self, **how) -> update.Outcome:
        return update.update(self.folder, str(self.up), "main", home=self.home, **how)

    def read(self, name: str) -> str:
        return (self.folder / name).read_text()

    def status(self) -> list[str]:
        return [line for line in git(self.folder, "status", "--porcelain").splitlines() if line]

    def head(self) -> str:
        return git(self.folder, "rev-parse", "HEAD")

    def everything(self) -> dict[str, bytes]:
        """Every file of the folder outside .git, to see that nothing was changed."""
        return {str(p.relative_to(self.folder)): p.read_bytes()
                for p in sorted(self.folder.rglob("*")) if p.is_file() and ".git" not in p.relative_to(self.folder).parts}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))                  # the updater must not need a name and e-mail set up in git
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return World(tmp_path)


ENGINE, MODES = "backend/hunterai/engine.py", "backend/hunterai/modes.py"


# ---- a plain copy

def test_a_folder_that_is_behind_is_brought_to_the_new_version(world):
    world.release("faster", **{ENGINE: "def run():\n    return 2\n"})
    new = world.release("tidy", **{"backend/hunterai/old.py": None, "backend/hunterai/hop.py": "hop = 1\n"})
    out = world.update()
    assert (out.status, out.applied, out.new) == ("updated", True, new) and out.old == world.first
    assert out.brought == [ENGINE, "backend/hunterai/hop.py", "backend/hunterai/old.py"]      # the files that changed on disk
    assert world.read(ENGINE) == "def run():\n    return 2\n" and world.read("backend/hunterai/hop.py") == "hop = 1\n"
    assert not (world.folder / "backend/hunterai/old.py").exists()
    assert world.head() == new and world.status() == []
    assert out.commits == ["tidy", "faster"]                   # what is new, newest first


def test_a_folder_that_has_the_newest_is_left_as_it_is(world):
    before = world.everything()
    out = world.update()
    assert (out.status, out.applied) == ("current", False) and world.everything() == before


def test_the_executable_bit_of_a_new_script_is_set(world):
    write(world.up, "scripts/update.sh", "#!/usr/bin/env bash\n", executable=True)
    commit(world.up, "an update script")
    world.update()
    assert (world.folder / "scripts/update.sh").stat().st_mode & stat.S_IXUSR


def test_names_with_spaces_and_other_alphabets_are_handled(world):
    world.release(**{"docs/mga gabay/pag-update ñ.md": "bago\n", "docs/mga gabay/luma.md": "x\n"})
    world.update()
    world.release(**{"docs/mga gabay/pag-update ñ.md": "mas bago\n", "docs/mga gabay/luma.md": None})
    world.update()
    assert world.read("docs/mga gabay/pag-update ñ.md") == "mas bago\n" and not (world.folder / "docs/mga gabay/luma.md").exists()


# ---- the owner's own changes

def test_the_owners_change_to_a_file_the_update_leaves_alone_stays(world):
    write(world.folder, MODES, RULES + "use my scanner first\n")
    world.release(**{ENGINE: "def run():\n    return 2\n"})
    out = world.update()
    assert world.read(MODES) == RULES + "use my scanner first\n" and world.read(ENGINE).endswith("return 2\n")
    assert world.status() == [f" M {MODES}"] and out.kept == [MODES] and out.merged == []


def test_the_owners_change_and_the_updates_change_to_one_file_are_both_kept(world):
    mine = RULES.replace("rule 2\n", "rule 2, and my scanner first\n")
    write(world.folder, MODES, mine)
    world.release(**{MODES: RULES.replace("rule 19\n", "rule 19, with a proxy\n")})
    out = world.update()
    assert world.read(MODES) == RULES.replace("rule 2\n", "rule 2, and my scanner first\n").replace("rule 19\n", "rule 19, with a proxy\n")
    assert out.merged == [MODES] and out.kept == [MODES] and world.status() == [f" M {MODES}"]
    assert (out.saved / MODES).read_text() == mine             # the owner's version from before, in case the merge is not what they want


def test_changes_to_the_same_lines_stop_the_update_and_nothing_is_changed(world):
    write(world.folder, MODES, RULES.replace("rule 5\n", "rule 5, my way\n"))
    write(world.folder, "notes.txt", "mine\n")
    world.release(**{MODES: RULES.replace("rule 5\n", "rule 5, their way\n"), ENGINE: "def run():\n    return 2\n"})
    before, head = world.everything(), world.head()
    out = world.update()
    assert (out.status, out.applied, out.conflicts) == ("conflicts", False, [MODES])
    assert world.everything() == before and world.head() == head          # not even the file that would have merged


def test_told_to_the_owners_version_is_put_beside_the_new_one(world):
    mine = RULES.replace("rule 5\n", "rule 5, my way\n")
    write(world.folder, MODES, mine)
    new = world.release(**{MODES: RULES.replace("rule 5\n", "rule 5, their way\n"), ENGINE: "def run():\n    return 2\n"})
    out = world.update(aside=True)
    assert (out.status, out.aside, out.conflicts) == ("updated", [MODES], [])
    assert world.read(MODES) == RULES.replace("rule 5\n", "rule 5, their way\n") and world.read(MODES + ".mine") == mine
    assert world.head() == new and world.status() == [f"?? {MODES}.mine"]


def test_a_file_of_the_owners_where_the_update_brings_one_stops_the_update(world):
    write(world.folder, "backend/hunterai/hop.py", "my own hop\n")
    world.release(**{"backend/hunterai/hop.py": "hop = 1\n"})
    before = world.everything()
    out = world.update()
    assert (out.status, out.conflicts) == ("conflicts", ["backend/hunterai/hop.py"]) and world.everything() == before


def test_a_file_the_owner_already_has_as_the_update_brings_it_is_no_obstacle(world):
    """The first time, the update script itself is fetched by hand."""
    write(world.folder, "scripts/update.sh", "#!/usr/bin/env bash\n", executable=True)
    write(world.up, "scripts/update.sh", "#!/usr/bin/env bash\n", executable=True)
    new = commit(world.up, "an update script")
    out = world.update()
    assert out.status == "updated" and world.head() == new and world.status() == []
    assert (out.merged, out.kept, out.saved) == ([], [], None)        # nothing of the owner's was at stake


def test_a_file_the_update_removes_stays_when_the_owner_changed_it(world):
    write(world.folder, "backend/hunterai/old.py", "gone = 'not if I use it'\n")
    world.release(**{"backend/hunterai/old.py": None})
    out = world.update()
    assert (out.status, out.conflicts) == ("conflicts", ["backend/hunterai/old.py"])
    assert world.read("backend/hunterai/old.py") == "gone = 'not if I use it'\n"


def test_a_file_changed_by_both_that_is_no_text_is_not_merged(world):
    write(world.up, "frontend/logo.png", b"\x89PNG\x00\x01\x02" + bytes(range(256)))
    commit(world.up, "a logo")
    world.update()
    write(world.folder, "frontend/logo.png", b"\x89PNG\x00mine" + bytes(range(256)))
    write(world.up, "frontend/logo.png", b"\x89PNG\x00theirs" + bytes(range(256)))
    commit(world.up, "a new logo")
    assert world.update().conflicts == ["frontend/logo.png"]
    assert (world.folder / "frontend/logo.png").read_bytes().startswith(b"\x89PNG\x00mine")


def test_what_is_not_part_of_hunterai_is_neither_touched_nor_read(world):
    """The owner's tool folder can hold hundreds of megabytes of programs."""
    tool = write(world.folder, "bin/nuclei", b"\x7fELF" + b"a program of the owner's" * 50, executable=True)
    write(world.folder, "notes/targets.txt", "shop.example\n")
    world.release(**{ENGINE: "def run():\n    return 2\n"})
    world.update()
    assert tool.read_bytes().startswith(b"\x7fELF") and world.read("notes/targets.txt") == "shop.example\n"
    name = git(world.folder, "hash-object", "bin/nuclei")
    known = subprocess.run(["git", "cat-file", "-e", name], cwd=world.folder, capture_output=True).returncode == 0
    assert not known                                           # git was never handed the file


# ---- only looking

def test_asked_only_to_look_it_says_what_an_update_would_do_and_changes_nothing(world):
    write(world.folder, MODES, RULES.replace("rule 2\n", "rule 2, mine\n"))
    new = world.release("with a proxy", **{MODES: RULES.replace("rule 19\n", "rule 19, with a proxy\n")})
    before, head = world.everything(), world.head()
    out = world.update(check=True)
    assert (out.status, out.applied, out.new, out.commits, out.merged) == ("updated", False, new, ["with a proxy"], [MODES])
    assert world.everything() == before and world.head() == head


# ---- a folder that is not a plain copy

def test_a_folder_with_commits_of_its_own_is_not_touched_unless_told(world):
    write(world.folder, ENGINE, "def run():\n    return 'mine'\n")
    commit(world.folder, "my own work")
    world.release(**{MODES: RULES + "more\n"})
    before, head = world.everything(), world.head()
    out = world.update()
    assert (out.status, out.applied, out.own_commits) == ("diverged", False, 1)
    assert world.everything() == before and world.head() == head


def mirrored(world) -> tuple[str, str]:
    """The folder as an update done by hand left it: the content of a newer version, committed here as a
    commit of the folder's own, with one file still carrying an uncommitted change of the owner's and a
    file of the owner's own beside it. Returns (the version it mirrors, the owner's file content)."""
    world.release("proxy", **{ENGINE: "def run():\n    return 2\n", "backend/hunterai/hop.py": "hop = 1\n"})
    there = world.release("window", **{"backend/hunterai/ollama.py": "ctx = 16384\n", MODES: RULES + "rule 21\n"})
    git(world.folder, "fetch", "-q", str(world.up), "+main:refs/remotes/gh/all")
    for name in (ENGINE, "backend/hunterai/hop.py", "backend/hunterai/ollama.py"):
        write(world.folder, name, git(world.folder, "show", f"gh/all:{name}") + "\n")
    commit(world.folder, "PRs mula sa GitHub")
    mine = RULES.replace("rule 2\n", "rule 2, and my scanner first\n") + "rule 21\n"
    write(world.folder, MODES, mine)                           # the new version of the file, with the owner's line in it
    write(world.folder, "backend/hunterai/phantomstrike.py", "def tools():\n    return []\n")
    return there, mine


def test_a_folder_that_mirrors_github_by_hand_is_put_on_its_history_with_the_owners_changes_kept(world):
    there, mine = mirrored(world)
    new = world.release("restart notice", **{"backend/hunterai/stale.py": "x = 1\n", MODES: RULES + "rule 21\nrule 22\n"})
    assert world.update().status == "diverged"
    out = world.update(reanchor=True)
    assert (out.status, out.new, out.own_commits) == ("updated", new, 1)
    assert world.head() == new and world.read("backend/hunterai/stale.py") == "x = 1\n"
    assert world.read(MODES) == RULES.replace("rule 2\n", "rule 2, and my scanner first\n") + "rule 21\nrule 22\n"
    assert world.status() == [f" M {MODES}", "?? backend/hunterai/phantomstrike.py"]      # what is the owner's, and nothing else
    assert out.brought == [MODES, "backend/hunterai/stale.py"] and out.merged == [MODES] and out.kept == [MODES]
    again = world.update()                                     # from here on it is a plain copy
    assert again.status == "current"


def test_a_mirrored_folder_that_already_holds_some_of_the_newest_files_still_gets_the_rest(world):
    """The first time, the update script and its module are fetched by hand: two of the newest files are
    there, the others are not. A folder like that is closer to the newest version than to the one it mirrors
    when files are only counted, and its older files then passed for changes of the owner's."""
    mirrored(world)
    new = world.release("an update script", **{"scripts/update.sh": "#!/usr/bin/env bash\n", "backend/hunterai/update.py": "u = 1\n",
                                                 "README.md": "how to update\n", ENGINE: "def run():\n    return 3\n"})
    write(world.folder, "scripts/update.sh", "#!/usr/bin/env bash\n")
    write(world.folder, "backend/hunterai/update.py", "u = 1\n")
    out = world.update(reanchor=True)
    assert out.status == "updated" and world.head() == new
    assert world.read("README.md") == "how to update\n" and world.read(ENGINE) == "def run():\n    return 3\n"
    assert world.status() == [f" M {MODES}", "?? backend/hunterai/phantomstrike.py"]
    assert out.brought == ["README.md", ENGINE] and out.kept == [MODES]


def test_the_owners_file_is_merged_on_top_of_the_version_it_was_made_from(world):
    """Which version that was is not recorded anywhere: it is the one the owner's file is closest to."""
    mirrored(world)                                            # the owner's rules are the second version with one line changed
    new = world.release("third rules", **{MODES: RULES.replace("rule 12\n", "rule 12, tightened\n") + "rule 21\nrule 22\n"})
    out = world.update(reanchor=True)
    assert out.status == "updated" and world.head() == new
    assert world.read(MODES) == (RULES.replace("rule 2\n", "rule 2, and my scanner first\n").replace("rule 12\n", "rule 12, tightened\n")
                                 + "rule 21\nrule 22\n")


def test_a_file_the_folder_lost_long_ago_or_never_had_is_brought_and_one_the_branch_dropped_is_removed(world):
    mirrored(world)
    (world.folder / "backend/hunterai/hop.py").unlink()        # unchanged upstream since: for all the folder shows, never received
    new = world.release("tidy", **{"backend/hunterai/old.py": None})
    out = world.update(reanchor=True)
    assert out.status == "updated" and world.head() == new
    assert world.read("backend/hunterai/hop.py") == "hop = 1\n" and not (world.folder / "backend/hunterai/old.py").exists()


def test_a_folder_whose_history_was_never_githubs_can_be_put_on_it_too(world, tmp_path):
    """A folder that began before the repository existed, or was unpacked and given a history of its own."""
    there = world.release("proxy", **{ENGINE: "def run():\n    return 2\n"})
    own = tmp_path / "grown-here"
    shutil.copytree(world.up, own, ignore=shutil.ignore_patterns(".git"))
    git(own, "init", "-q", "-b", "main")
    commit(own, "local dev history")
    write(own, MODES, RULES.replace("rule 2\n", "rule 2, and my scanner first\n"))
    write(own, "backend/hunterai/phantomstrike.py", "def tools():\n    return []\n")
    new = world.release("window", **{"backend/hunterai/ollama.py": "ctx = 16384\n"})
    assert update.update(own, str(world.up), "main", home=world.home).status == "diverged"
    out = update.update(own, str(world.up), "main", home=world.home, reanchor=True)
    assert (out.status, out.new, out.own_commits, out.brought) == ("updated", new, 1, ["backend/hunterai/ollama.py"])
    assert git(own, "rev-parse", "HEAD") == new and (own / "backend/hunterai/ollama.py").read_text() == "ctx = 16384\n"
    assert git(own, "status", "--porcelain").splitlines() == [f" M {MODES}", "?? backend/hunterai/phantomstrike.py"]


def test_a_mirrored_folder_that_already_has_the_newest_content_is_only_put_on_the_history(world):
    there, mine = mirrored(world)
    before = world.everything()
    out = world.update(reanchor=True)
    assert (out.status, out.applied, out.new, out.brought) == ("updated", True, there, [])
    assert world.everything() == before and world.head() == there
    assert world.status() == [f" M {MODES}", "?? backend/hunterai/phantomstrike.py"]


def test_a_folder_that_is_no_copy_of_the_branch_is_not_put_on_its_history(world):
    """Asked to re-anchor onto a branch that does not hold the folder's code, for one."""
    for n in range(update.FAR + 5):
        write(world.folder, f"backend/hunterai/mod_{n}.py", f"n = {n}\n")
        write(world.up, f"backend/hunterai/mod_{n}.py", f"n = 'another {n}'\n")
    commit(world.folder, "a lot of my own")
    commit(world.up, "a lot of theirs")
    before, head = world.everything(), world.head()
    out = world.update(reanchor=True)
    assert (out.status, out.applied) == ("foreign", False) and world.everything() == before and world.head() == head
    assert out.apart == update.FAR + 5


def test_a_folder_that_is_newer_than_the_branch_is_left_alone(world):
    write(world.folder, ENGINE, "def run():\n    return 3\n")
    commit(world.folder, "not merged on GitHub yet")
    before = world.everything()
    out = world.update()
    assert (out.status, out.applied, out.own_commits) == ("ahead", False, 1) and world.everything() == before


# ---- what stops it before it starts

def test_a_merge_that_was_left_unfinished_is_finished_first(world):
    world.release(**{ENGINE: "def run():\n    return 2\n"})
    (world.folder / ".git" / "MERGE_HEAD").write_text(world.first + "\n")
    with pytest.raises(update.Refused, match="unfinished"):
        world.update()


def test_a_source_that_cannot_be_reached_is_said_and_nothing_is_changed(world):
    before = world.everything()
    with pytest.raises(update.Refused, match="Could not fetch main from"):
        update.update(world.folder, str(world.up) + "-gone", "main", home=world.home)
    assert world.everything() == before


def test_a_folder_that_is_no_checkout_is_said(tmp_path, world):
    plain = tmp_path / "unpacked"
    plain.mkdir()
    with pytest.raises(update.Refused, match="not a git checkout"):
        update.update(plain, str(world.up), "main", home=world.home)


# ---- stopping and starting HunterAI around the change

def test_hunterai_is_stopped_after_the_update_is_known_to_fit_and_before_a_file_changes(world):
    world.release(**{ENGINE: "def run():\n    return 2\n"})
    seen = []
    world.update(before_change=lambda: seen.append(world.read(ENGINE)))
    assert seen == ["def run():\n    return 1\n"] and world.read(ENGINE).endswith("return 2\n")


@pytest.mark.parametrize("how", ["current", "conflicts", "check"])
def test_hunterai_is_left_running_when_nothing_will_change(world, how):
    if how != "current":
        write(world.folder, MODES, RULES.replace("rule 5\n", "rule 5, my way\n"))
        world.release(**{MODES: RULES.replace("rule 5\n", "rule 5, their way\n") if how == "conflicts" else RULES + "more\n"})
    seen = []
    world.update(check=how == "check", before_change=lambda: seen.append("stopped"))
    assert seen == []


PS = """\
    1 /sbin/init
  812 bash scripts/start.sh
  815 uv run python -m hunterai.worker
  816 /home/dave/Desktop/hunterai/backend/.venv/bin/python3 -m hunterai.worker
  820 uv run python -m hunterai
  821 /home/dave/Desktop/hunterai/backend/.venv/bin/python3 -m hunterai
  900 uv run python -m hunterai.update --reanchor
  901 /home/dave/Desktop/hunterai/backend/.venv/bin/python3 -m hunterai.update --reanchor
  950 vim backend/hunterai/update.py
  960 python3 -m hunterai_tools
  970 python3 -c import hunterai
"""


def test_the_processes_of_hunterai_are_the_server_and_the_worker_and_not_this_update():
    found = update.processes(PS, own=901)
    assert [(p.pid, p.kind) for p in found] == [(816, "worker"), (821, "server")]


def test_the_server_is_asked_to_stop_first_and_the_worker_only_if_it_is_still_there():
    alive, sent = {816, 821}, []

    def signal_(pid, sig):
        sent.append((pid, sig.name))
        if pid == 821:
            alive.clear()                                      # the start script stops its worker when the server ends
    update.stop(update.processes(PS, own=901), signal=signal_, alive=lambda pid: pid in alive, wait=0.01, patience=0.2)
    assert sent == [(821, "SIGINT")]


def test_a_process_that_does_not_stop_when_asked_is_ended():
    alive, sent = {816, 821}, []

    def signal_(pid, sig):
        sent.append((pid, sig.name))
        if sig.name == "SIGKILL":
            alive.discard(pid)
    update.stop(update.processes(PS, own=901), signal=signal_, alive=lambda pid: pid in alive, wait=0.01, patience=0.05)
    assert sent == [(821, "SIGINT"), (821, "SIGTERM"), (821, "SIGKILL"), (816, "SIGTERM"), (816, "SIGKILL")]


# ---- the command itself

def run_main(world, monkeypatch, *args, running=(), stops=True):
    """The command with stand-ins for HunterAI's processes. Returns (its exit code, what it said, what it did)."""
    said, events, alive = [], [], {p.pid for p in running}

    def stop(procs, **kw):
        events.append(("stopped", [p.pid for p in procs], world.read(ENGINE)))
        if stops:
            alive.clear()
    monkeypatch.setattr(update, "running", lambda root: list(running))
    monkeypatch.setattr(update, "stop", stop)
    monkeypatch.setattr(update, "_alive", lambda pid: pid in alive)
    code = update.main(["--from", str(world.up), *args], root=world.folder, home=world.home, out=said.append)
    return code, "\n".join(said), events


def test_one_command_stops_hunterai_updates_the_folder_and_asks_for_it_to_be_started(world, monkeypatch):
    world.release("faster", **{ENGINE: "def run():\n    return 2\n"})
    code, said, events = run_main(world, monkeypatch, running=[update.Proc(821, "server")])
    assert code == update.START                                # the script that called it runs the start script next
    assert events == [("stopped", [821], "def run():\n    return 1\n")] and world.read(ENGINE).endswith("return 2\n")
    assert "Stopping HunterAI" in said and "Updated HunterAI" in said and "- faster" in said


def test_hunterai_is_started_after_an_update_even_when_it_was_not_running(world, monkeypatch):
    world.release(**{ENGINE: "def run():\n    return 2\n"})
    code, said, events = run_main(world, monkeypatch)
    assert code == update.START and events == [] and "Stopping" not in said


def test_told_not_to_it_does_not_ask_for_a_start(world, monkeypatch):
    world.release(**{ENGINE: "def run():\n    return 2\n"})
    code, said, events = run_main(world, monkeypatch, "--no-start", running=[update.Proc(821, "server")])
    assert code == 0 and [e[0] for e in events] == ["stopped"] and "scripts/start.sh" in said


def test_a_hunterai_that_cannot_be_stopped_keeps_its_files(world, monkeypatch):
    """The files of a running server are not changed under it."""
    world.release(**{ENGINE: "def run():\n    return 2\n"})
    before = world.everything()
    code, said, events = run_main(world, monkeypatch, running=[update.Proc(821, "server")], stops=False)
    assert code == 2 and "still running" in said and world.everything() == before


def test_with_nothing_new_it_says_so_and_touches_neither_the_folder_nor_hunterai(world, monkeypatch):
    code, said, events = run_main(world, monkeypatch, running=[update.Proc(821, "server")])
    assert code == 0 and events == [] and "up to date" in said


def test_files_that_cannot_be_merged_are_named_with_what_can_be_done(world, monkeypatch):
    write(world.folder, MODES, RULES.replace("rule 5\n", "rule 5, my way\n"))
    world.release(**{MODES: RULES.replace("rule 5\n", "rule 5, their way\n")})
    code, said, events = run_main(world, monkeypatch, running=[update.Proc(821, "server")])
    assert code == 1 and events == []
    assert MODES in said and "Nothing was changed" in said and "--aside" in said


def test_a_folder_with_commits_of_its_own_is_told_about_reanchor(world, monkeypatch):
    mirrored(world)
    code, said, events = run_main(world, monkeypatch)
    assert code == 1 and events == [] and "--reanchor" in said and "Nothing was changed" in said
    new = world.release("restart notice", **{"backend/hunterai/stale.py": "x = 1\n", ENGINE: "def run():\n    return 3\n"})
    code, said, events = run_main(world, monkeypatch, "--reanchor", "--check")
    assert code == 0 and f"An update would bring HunterAI to {new[:7]}, the newest on main: 2 file(s) would change." in said
    code, said, events = run_main(world, monkeypatch, "--reanchor", "--no-start")
    assert code == 0 and f"Brought HunterAI to {new[:7]}, the newest on main: 2 file(s) changed." in said
    assert "now stands on" in said and f"Your own changes are still in: {MODES}." in said
    assert "phantomstrike" not in said                         # a file of the owner's own is not a change to HunterAI's
    code, said, events = run_main(world, monkeypatch, "--no-start")
    assert code == 0 and "up to date" in said


def test_what_stops_the_update_before_it_starts_is_said_plainly(world, monkeypatch):
    code, said, events = run_main(world, monkeypatch, "--branch", "no-such-branch")
    assert code == 2 and events == [] and "Could not fetch no-such-branch" in said
