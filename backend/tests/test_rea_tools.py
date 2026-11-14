import os

from hunterai import config, mcp_manager, modes, rea_tools


def tree(root, *files):
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)


def test_nothing_in_the_folder_means_rea_starts_as_before(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert rea_tools.folder() == config.home() / "rea"
    assert rea_tools.env() == {} and rea_tools.found() == []
    assert mcp_manager.default_servers()["rea"].env == {}


def test_what_the_owner_put_in_the_folder_is_handed_to_rea(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    root = rea_tools.folder()
    real = tmp_path / "ghidra_12.1.2_PUBLIC"
    tree(real, "support/analyzeHeadless")
    os.symlink(real, root / "ghidra")                                   # a link to an installation elsewhere
    tree(root, "jdk/jdk-21/bin/java", "hopper/bin/Hopper", "ilspycmd", "adb", "bin/jadx", "bin/rizin")
    (root / "bin" / "notes.txt").write_text("x")
    env = rea_tools.env()
    assert env == {
        "GHIDRA_INSTALL_DIR": str(root / "ghidra"), "JAVA_HOME": str(root / "jdk" / "jdk-21"),
        "HOPPER_LAUNCHER_PATH": str(root / "hopper" / "bin" / "Hopper"), "REA_ILSPY_CMD_PATH": str(root / "ilspycmd"),
        "REA_ADB_PATH": str(root / "adb"), "PATH": f"{root / 'bin'}:{config.home() / 'bin'}:/usr/bin:/bin"}
    assert rea_tools.found() == ["ghidra", "jdk", "hopper", "ilspycmd", "adb", "jadx", "rizin"]
    assert mcp_manager.default_servers()["rea"].env == env
    assert mcp_manager.load_config()[0]["rea"].env == env


def test_a_folder_that_is_not_the_program_is_not_handed_over(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    root = rea_tools.folder()
    (root / "ghidra").mkdir(); (root / "ghidra" / "readme.txt").write_text("not ghidra")
    (root / "hopper").mkdir()
    (root / "adb").write_text("not executable")
    assert rea_tools.env() == {} and rea_tools.found() == []
    os.symlink("/nonexistent/ghidra", root / "jdk")
    assert rea_tools.env() == {}


def test_the_model_is_asked_to_keep_to_the_users_language():
    for mode in ("plan", "pentesting", "coding"):
        assert "language the user writes in" in modes.build_system(mode, None, ["read"])
        assert "language the user writes in" in modes.build_system(mode, None, [])
