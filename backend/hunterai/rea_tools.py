"""The optional programs of the bundled `rea` server, in one place: `~/.hunterai/rea`.
What the owner puts there (the program, or a link to where it is installed) is handed to
REA when it starts, as the environment variable REA reads for it:

    ghidra/     a Ghidra installation (it holds support/analyzeHeadless)   GHIDRA_INSTALL_DIR
    jdk/        a JDK for Ghidra (it holds bin/java)                       JAVA_HOME
    hopper/     Hopper (it holds bin/Hopper), or the launcher itself       HOPPER_LAUNCHER_PATH
    ilspycmd    the ILSpy command line program                             REA_ILSPY_CMD_PATH
    adb         Android's adb                                              REA_ADB_PATH
    bin/        anything REA finds by name: jadx, rizin, apktool, tshark   first on its PATH

A folder may hold the installation itself or exactly one folder that does (an unpacked
archive). Nothing is downloaded, and a name that does not hold what it should is ignored."""
from __future__ import annotations

import os
from pathlib import Path

from . import config, toolbox


def folder() -> Path:
    p = config.home() / "rea"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _runnable(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def _holding(root: Path, inside: str) -> Path | None:
    """`root`, or the one folder in it, that holds the runnable file `inside`."""
    try:
        if not root.is_dir():
            return None
        if _runnable(root / inside):
            return root
        subs = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return None
    return subs[0] if len(subs) == 1 and _runnable(subs[0] / inside) else None


def _parts() -> dict[str, tuple[str, str]]:
    """name -> (variable, value) for what is in the folder and is what it should be."""
    root, out = folder(), {}
    ghidra = _holding(root / "ghidra", "support/analyzeHeadless")
    if ghidra:
        out["ghidra"] = ("GHIDRA_INSTALL_DIR", str(ghidra))
    jdk = _holding(root / "jdk", "bin/java")
    if jdk:
        out["jdk"] = ("JAVA_HOME", str(jdk))
    hopper = root / "hopper"
    launcher = hopper if _runnable(hopper) else next(
        (h / "bin" / n for h in [_holding(hopper, "bin/Hopper") or _holding(hopper, "bin/hopper")] if h
         for n in ("Hopper", "hopper") if _runnable(h / "bin" / n)), None)
    if launcher:
        out["hopper"] = ("HOPPER_LAUNCHER_PATH", str(launcher))
    for name, variable in (("ilspycmd", "REA_ILSPY_CMD_PATH"), ("adb", "REA_ADB_PATH")):
        if _runnable(root / name):
            out[name] = (variable, str(root / name))
    return out


def _programs() -> list[str]:
    try:
        return sorted(p.name for p in (folder() / "bin").iterdir() if _runnable(p))
    except OSError:
        return []


def found() -> list[str]:
    """The names of what REA is given, for the setup check."""
    return [*_parts(), *_programs()]


def env() -> dict[str, str]:
    """The environment the rea server is started with on top of the usual one; {} when the folder holds nothing."""
    out = {variable: value for variable, value in _parts().values()}
    if _programs():
        base = toolbox.path_with_folder(os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin")
        out["PATH"] = os.pathsep.join([str(folder() / "bin"), base])
    return out
