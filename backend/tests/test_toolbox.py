import os

from hunterai import config, modes, procs, toolbox
from hunterai.providers.base import TextDelta
from tests.test_api import client  # noqa: F401 (client is a fixture)
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns


def program(name: str, executable: bool = True):
    path = toolbox.folder() / name
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(0o755 if executable else 0o644)
    return path


def test_the_tool_folder_lists_the_programs_the_owner_put_there():
    assert toolbox.folder() == config.home() / "bin" and toolbox.installed() == []
    program("nuclei"); program("mytool"); program("notes.txt", executable=False); program("caido-cli")
    program("bad name;rm"); (toolbox.folder() / "sub").mkdir()
    os.symlink("/nonexistent/httpx", toolbox.folder() / "httpx")          # a link to nothing is not a program
    got = toolbox.installed()
    assert [(t["name"], t["known"]) for t in got] == [("mytool", False), ("nuclei", True)]
    assert "template" in got[1]["about"] and got[0]["about"] == ""


def test_commands_find_the_tool_folder_first(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert procs.child_env()["PATH"] == f"{toolbox.folder()}:/usr/bin:/bin"
    monkeypatch.setenv("PATH", f"{toolbox.folder()}:/usr/bin")              # already there: not added twice
    assert procs.child_env()["PATH"] == f"{toolbox.folder()}:/usr/bin"


def test_the_model_is_told_which_programs_it_has_only_with_a_shell():
    from hunterai.tools import shell
    runs_commands = shell.tools()[0].name                  # the tool as the app offers it to a model
    assert runs_commands == "execute"
    assert "tool folder" not in modes.build_system("pentesting", None, [runs_commands])
    program("nuclei"); program("mytool")
    told = modes.build_system("pentesting", None, [runs_commands, "read"])
    assert "nuclei (vulnerability scanner driven by templates)" in told and "mytool" in told
    assert "nuclei" not in modes.build_system("pentesting", None, ["read", "process_output"])
    assert "nuclei" not in modes.build_system("pentesting", None, [])


async def test_a_run_tells_the_model_its_programs(conn, setup):
    from hunterai.tools import registry, shell
    for t in shell.tools():
        registry.register(t)
    program("nuclei")
    p = Turns("abliteration", [TextDelta("ok")])
    use(abliteration=p)
    await go(conn, *await setup())
    assert "Programs in the owner's tool folder" in p.calls[0]["system"] and "nuclei (vulnerability scanner" in p.calls[0]["system"]


async def test_the_checkup_names_the_tool_folder(client, monkeypatch):
    from hunterai.api import system

    async def fetch(url):
        return None
    monkeypatch.setattr(system, "_fetch", fetch)
    item = {i["id"]: i for i in (await client.get("/api/system/checkup")).json()["items"]}["toolbox"]
    assert not item["ok"] and not item["required"] and str(toolbox.folder()) in item["fix"]
    program("subfinder"); program("nuclei")
    item = {i["id"]: i for i in (await client.get("/api/system/checkup")).json()["items"]}["toolbox"]
    assert item["ok"] and "nuclei, subfinder" in item["detail"]
    got = (await client.get("/api/system/toolbox")).json()
    assert got["folder"] == str(toolbox.folder()) and [t["name"] for t in got["tools"]] == ["nuclei", "subfinder"]
    assert "httpx" in got["missing"] and "nuclei" not in got["missing"]


def test_the_bin_folder_next_to_the_code_comes_first(monkeypatch):
    project = toolbox.PROJECT_BIN
    assert toolbox.folders() == [toolbox.folder()] and toolbox.primary() == toolbox.folder()      # it does not exist: only the data folder
    project.mkdir()
    assert toolbox.folders() == [project, toolbox.folder()] and toolbox.primary() == project

    def put(root, name):
        (root / name).write_text("#!/bin/sh\n"); (root / name).chmod(0o755)
    put(project, "nuclei"); put(project, "gitleaks"); put(toolbox.folder(), "nuclei"); put(toolbox.folder(), "mytool")
    got = toolbox.installed()
    assert [t["name"] for t in got] == ["gitleaks", "mytool", "nuclei"] and "secrets" in got[0]["about"]
    monkeypatch.setenv("PATH", f"/usr/bin:{toolbox.folder()}:/bin")
    assert procs.child_env()["PATH"] == f"{project}:{toolbox.folder()}:/usr/bin:/bin"              # both first, the project's before the other
    assert "gitleaks (finds secrets" in modes.build_system("pentesting", None, ["execute"])


def test_where_a_program_of_the_folder_is(monkeypatch):
    assert toolbox.where("nuclei") is None
    own = program("nuclei")
    assert toolbox.where("nuclei") == own
    program("notes.txt", executable=False)
    assert toolbox.where("notes.txt") is None                        # a file that cannot be run is no program
    assert toolbox.where("../nuclei") is None and toolbox.where("") is None
    project = toolbox.PROJECT_BIN
    project.mkdir()
    (project / "nuclei").write_text("#!/bin/sh\n")
    (project / "nuclei").chmod(0o755)
    assert toolbox.where("nuclei") == project / "nuclei"             # the folder that comes first, as in a command

