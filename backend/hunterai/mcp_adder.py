"""The MCP adder: from a repository, a package, a folder, or a pasted configuration to a
proposal the owner can approve. Inspecting fetches code and reads it; nothing from it runs.
Installing runs only the steps that were shown, and starting the server is a separate,
later act (the switch in Settings)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import config, mcp_check, mcp_manager
from .db import new_id, now
from .kb import archives, gitsrc, limits, tools

MAX_PACKAGE_BYTES = 100 * 1024**2
MAX_CLONE_BYTES = 500 * 1024**2
INSTALL_TIMEOUT = 600.0
NPM_REGISTRY = "https://registry.npmjs.org"
PYPI = "https://pypi.org/pypi"
TARBALL_HOSTS = ("registry.npmjs.org", "files.pythonhosted.org")
_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
_PY_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_VERSION = r"[A-Za-z0-9][A-Za-z0-9._+!-]*"
_ENV_NAME = re.compile(r"(?:process\.env\.([A-Z][A-Z0-9_]{2,})|process\.env\[['\"]([A-Z][A-Z0-9_]{2,})['\"]\]|"
                       r"os\.environ(?:\.get)?[\[(]\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]|os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]|"
                       r"os\.(?:Getenv|LookupEnv)\(\s*\"([A-Z][A-Z0-9_]{2,})\")")
_ENV_IGNORE = {"PATH", "HOME", "USER", "SHELL", "PWD", "NODE_ENV", "DEBUG", "PORT", "HOST", "TMPDIR", "TEMP", "LANG", "TERM",
               "CI", "LOG_LEVEL", "PYTHONPATH", "VIRTUAL_ENV", "XDG_CONFIG_HOME", "APPDATA", "USERPROFILE"}


_ENV_NEVER = ("LD_", "DYLD_", "NODE_OPTIONS", "PYTHONSTARTUP", "PYTHONPATH", "BASH_ENV", "npm_config_", "NPM_CONFIG_", "UV_")
RUNS_CODE = "This step runs code from the package itself, before the server is ever switched on."


class AdderError(Exception):
    pass


def _dir(*parts: str) -> Path:
    p = config.home().joinpath("mcp", *parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def classify(source: str) -> dict:
    """What kind of source this is: git, npm, pypi, folder, or config. AdderError otherwise."""
    s = (source or "").strip()
    if not s:
        raise AdderError("give a repository URL, a package, a folder, or a configuration")
    if s.startswith("{"):
        try:
            data = json.loads(s)
        except ValueError:
            raise AdderError("the pasted configuration is not valid JSON") from None
        return {"kind": "config", "data": data}
    m = re.fullmatch(rf"(?:npm:|https://(?:www\.)?npmjs\.com/package/)({_NPM_NAME})(?:@({_VERSION})|/v/({_VERSION}))?/?", s)
    if m or re.fullmatch(rf"@[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*(?:@{_VERSION})?", s):
        if not m:
            name, _, version = s[1:].partition("@")
            return {"kind": "npm", "name": "@" + name, "version": version or None}
        return {"kind": "npm", "name": m.group(1), "version": m.group(2) or m.group(3)}
    m = re.fullmatch(rf"(?:pypi:|https://pypi\.org/project/)({_PY_NAME})(?:==({_VERSION})|/({_VERSION}))?/?", s)
    if m:
        return {"kind": "pypi", "name": m.group(1), "version": m.group(2) or m.group(3)}
    if s.startswith("/"):
        p = Path(s)
        try:
            real = p.resolve(strict=True)
        except (OSError, RuntimeError):
            raise AdderError("that folder does not exist") from None
        home = config.home().resolve()
        if not real.is_dir() or real == home or home in real.parents:
            raise AdderError("give a folder that holds the server's code (not HunterAI's own data folder)")
        return {"kind": "folder", "path": str(real)}
    try:
        url, label = gitsrc.validate(s)
        return {"kind": "git", "url": url, "label": label}
    except ValueError:
        pass
    raise AdderError("not understood. Use a GitHub or GitLab repository URL, npm:<package>, pypi:<package>, "
                     "an npmjs.com or pypi.org package page, an absolute folder path, or a JSON configuration")


def safe_name(raw: str) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", (raw or "").lower().split("/")[-1]).strip("_")
    name = re.sub(r"^(mcp_server_|server_|mcp_)|(_mcp_server|_mcp|_server)$", "", name) or name
    if not name or not name[0].isalpha():
        name = "s_" + name
    return name[:24].rstrip("_") or "server"


async def _json(client: httpx.AsyncClient, url: str) -> dict:
    try:
        r = await client.get(url)
    except httpx.HTTPError as e:
        raise AdderError(f"the package registry could not be reached ({type(e).__name__})") from None
    if r.status_code == 404:
        raise AdderError("no such package or version in the registry")
    if r.status_code != 200:
        raise AdderError(f"the package registry answered {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise AdderError("the package registry sent something unreadable") from None


async def _download(client: httpx.AsyncClient, url: str, dest: Path) -> None:
    host = httpx.URL(url).host
    if httpx.URL(url).scheme != "https" or host not in TARBALL_HOSTS:
        raise AdderError(f"the registry points at an unexpected download host ({host})")
    limits.require_space(MAX_PACKAGE_BYTES)
    size = 0
    try:
        async with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise AdderError(f"the package download answered {r.status_code}")
            with open(dest, "wb") as f:
                async for part in r.aiter_bytes():
                    size += len(part)
                    if size > MAX_PACKAGE_BYTES:
                        raise AdderError(f"the package is larger than {MAX_PACKAGE_BYTES} bytes")
                    f.write(part)
    except httpx.HTTPError as e:
        raise AdderError(f"the package could not be downloaded ({type(e).__name__})") from None


async def _unpack(archive: Path, into: Path) -> tuple[Path, list[str]]:
    """(the package's own folder, what could not be unpacked). Archives inside the package are
    unpacked next to it under `into`, so a check of `into` reads them too."""
    found, problems = await archives.unpack(archive, archive.name, into, limits.Budget())
    archive.unlink(missing_ok=True)
    outer = [f for f in found if f.uri.count("!/") == 1]      # members of the package itself, not of an archive in it
    if not outer:
        raise AdderError("the package could not be unpacked: " + (problems[0].reason if problems else "it is empty"))
    base = into / outer[0].path.relative_to(into).parts[0]
    entries = list(base.iterdir())       # one folder holding everything (npm's "package/", an sdist's "name-1.0/")
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else base
    return root, [f"{q.uri}: {q.reason}" for q in problems][:50]


async def fetch(src: dict, work: Path, client: httpx.AsyncClient | None = None) -> tuple[Path | None, dict]:
    """Bring the code to this machine without running it. Returns (folder or None, facts)."""
    kind = src["kind"]
    if kind == "config":
        return None, {}
    if kind == "folder":
        return Path(src["path"]), {"label": src["path"]}
    if kind == "git":
        try:
            await gitsrc.clone(src["url"], work / "code", MAX_CLONE_BYTES)
        except gitsrc.GitError as e:
            raise AdderError(str(e)) from None
        return work / "code", {"label": src["label"]}
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), trust_env=False, follow_redirects=False)
    try:
        if kind == "npm":
            meta = await _json(client, f"{NPM_REGISTRY}/{src['name'].replace('/', '%2F')}")
            version = src.get("version") or (meta.get("dist-tags") or {}).get("latest")
            entry = (meta.get("versions") or {}).get(version or "")
            if not entry:
                raise AdderError("no such package or version in the registry")
            await _download(client, (entry.get("dist") or {}).get("tarball") or "", work / "package.tgz")
            root, missed = await _unpack(work / "package.tgz", work / "code")
            return root, {"label": f"{src['name']}@{version}", "version": version, "scan_root": work / "code", "unpack_problems": missed}
        meta = await _json(client, f"{PYPI}/{src['name']}/{src['version']}/json" if src.get("version") else f"{PYPI}/{src['name']}/json")
        version = (meta.get("info") or {}).get("version")
        files = meta.get("urls") or []
        pick = next((f for f in files if f.get("packagetype") == "sdist" and str(f.get("filename", "")).endswith(".tar.gz")), None) \
            or next((f for f in files if str(f.get("filename", "")).endswith(".whl")), None)
        if not pick or not version:
            raise AdderError("the package has no source archive or wheel to look at")
        name = "package.tar.gz" if pick["filename"].endswith(".tar.gz") else "package.zip"
        await _download(client, pick["url"], work / name)
        root, missed = await _unpack(work / name, work / "code")
        return root, {"label": f"{src['name']}=={version}", "version": version, "scan_root": work / "code", "unpack_problems": missed}
    finally:
        if own:
            await client.aclose()


def _readme_entries(root: Path) -> list[dict]:
    """Every server entry in the README's JSON blocks, in order: one with a "command" starts
    a program, one with a "url" names a server someone hosts."""
    out: list[dict] = []
    for name in ("README.md", "readme.md", "README.MD", "Readme.md"):
        f = root / name
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")[:400_000]
        # Every fenced block is matched, whatever its language, so that the end of a shell block is
        # not taken for the start of a JSON one.
        for lang, block in re.findall(r"^[ \t]*```([^\n`]*)\n(.*?)^[ \t]*```[ \t]*$", text, re.S | re.M):
            if lang.strip().lower() not in ("", "json", "jsonc", "json5"):
                continue
            if "mcpServers" not in block and '"command"' not in block and '"url"' not in block:
                continue
            plain = re.sub(r"^\s*//.*$", "", block, flags=re.M)
            data = None
            for candidate in (plain, "{" + plain + "}"):        # a README often shows the entry alone: "name": {...}
                try:
                    data = json.loads(candidate)
                    break
                except ValueError:
                    continue
            if not isinstance(data, dict):
                continue
            inner = data.get("mcp") if isinstance(data.get("mcp"), dict) else {}
            servers = next((s for s in (data.get("mcpServers"), data.get("servers"), inner.get("servers"))
                            if isinstance(s, dict) and s), None)
            if servers is None and not isinstance(data.get("command"), str) and not isinstance(data.get("url"), str):
                # only entries that start a program: any JSON object may hold a "url" (a package.json's repository)
                servers = {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("command"), str)}
                if not servers:
                    continue
            for entry in (servers.values() if servers else [data]):
                if isinstance(entry, dict) and (isinstance(entry.get("command"), str) or isinstance(entry.get("url"), str)) \
                        and len(out) < 40:
                    out.append(entry)
        break
    return out


def _readme_config(root: Path) -> dict | None:
    """The first server entry of the README that starts a program, if there is one."""
    return next((e for e in _readme_entries(root) if isinstance(e.get("command"), str)), None)


def _hosted(root: Path) -> tuple[str, list[str]] | None:
    """(address, header names) of the first hosted server the README names: https, no user name in it."""
    for entry in _readme_entries(root):
        url = entry.get("url")
        if not isinstance(url, str) or isinstance(entry.get("command"), str) or len(url) > 500:
            continue
        try:
            parts = urlsplit(url)
            ok = parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
        except ValueError:
            ok = False
        if ok:        # the header names: from the first entry for this address that has any
            headers = next((e["headers"] for e in _readme_entries(root)
                            if e.get("url") == url and isinstance(e.get("headers"), dict) and e["headers"]), {})
            return url, [k for k in headers if isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", k)][:5]
    return None


def _go_programs(root: Path) -> list[str]:
    """The folders of a Go module that build a program: "." and the ones under cmd/."""
    def is_main(folder: Path) -> bool:
        try:
            for f in sorted(folder.glob("*.go"))[:200]:
                if not f.name.endswith("_test.go") and f.is_file() and not f.is_symlink() \
                        and re.search(r"^package main\b", f.read_bytes()[:20_000].decode("utf-8", "replace"), re.M):
                    return True
        except OSError:
            pass
        return False
    cmd = root / "cmd"
    found = [d.name for d in sorted(cmd.iterdir()) if d.is_dir() and not d.is_symlink()
             and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", d.name) and is_main(d)] if cmd.is_dir() else []
    return ([*found, "."] if is_main(root) else found)[:50]


def _version(text: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"(?:go)?(\d{1,3})\.(\d{1,3})(?:\.(\d{1,3}))?", text.strip())
    return tuple(int(g or 0) for g in m.groups()) if m else None


def _go_version() -> tuple[int, ...] | None:
    """The version of the go on this machine, or None when there is none."""
    program = shutil.which("go")
    if program is None:
        return None
    try:
        out = subprocess.run([program, "env", "GOVERSION"], capture_output=True, text=True, timeout=10,   # noqa: S603
                             env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
                                  "GOTOOLCHAIN": "local"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _version(out.stdout)


def _go_needs(root: Path) -> str | None:
    """The Go version the module asks for: its `toolchain` line, else its `go` line."""
    try:
        text = (root / "go.mod").read_text(errors="replace")[:20_000]
    except OSError:
        return None
    for pattern in (r"^toolchain\s+go(\d[\d.]*)\s*$", r"^go\s+(\d[\d.]*)\s*$"):
        m = re.search(pattern, text, re.M)
        if m and _version(m.group(1)):
            return m.group(1)
    return None


def _go_proposal(root: Path, label: str, base: dict, notes: list[str]) -> dict | None:
    """Build the module's server with `go build` and start the program that makes. None when
    the module builds no program."""
    programs = _go_programs(root)
    if not programs:
        return None
    repo = re.sub(r"[^A-Za-z0-9._-]", "", label.rstrip("/").rsplit("/", 1)[-1]).lstrip(".-") or "server"
    named = [p for p in programs if p != "."]
    mcp = [p for p in named if "mcp" in p.lower()]
    pick = repo if repo in named else "." if "." in programs else named[0] if len(named) == 1 else mcp[0] if len(mcp) == 1 else None
    if pick is None:
        raise AdderError(f"this Go repository builds several programs ({', '.join(named)}) and none is named after it: "
                         "build the one that is the server yourself and add it by hand")
    program = repo if pick == "." else pick
    binary = str(root / ".hunterai-bin" / program)
    said = next((e for e in _readme_entries(root) if isinstance(e.get("command"), str) and Path(e["command"]).name == program), {})
    args = [str(a) for a in (said.get("args") or []) if isinstance(a, (str, int, float))][:20] if isinstance(said.get("args"), list) else []
    runs = ("go build downloads this module's dependencies and compiles them into the server. It runs none "
            "of their code (cgo is off), and loads the processor for some minutes on a large project.")
    needs, have, env = _go_needs(root), _go_version(), {}
    if have is None:
        notes = [*notes, "go is not installed on this machine: the build step will fail until it is."]
    elif needs and _version(needs) > have:
        # A newer Go is named exactly and fetched by go itself, from the Go project, checked against Go's checksum database.
        env = {"GOTOOLCHAIN": f"go{needs}"}
        runs += (f" This module asks for Go {needs} and this machine has {'.'.join(map(str, have))}: go downloads that "
                 "toolchain from the Go project first (about 70 MB, kept in your Go cache) and builds with it.")
    return {**base, "name": safe_name(program), "command": binary, "args": args, "cwd": str(root),
            "install": [["go", "build", "-trimpath", "-p", "2", "-o", binary, "./cmd/" + pick if pick != "." else "."]],
            "install_env": env, "install_runs_code": [runs],
            "notes": [*notes, *(["The start options are the README's own for this program: " + " ".join(args)[:300]] if args else [])]}


def env_names(root: Path, readme: dict | None) -> list[str]:
    names: dict[str, None] = {}
    said = [readme or {}, *(e for e in _readme_entries(root) if isinstance(e.get("command"), str))]
    for k in (k for e in said for k in (e.get("env") if isinstance(e.get("env"), dict) else {})):
        if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k) and k not in _ENV_IGNORE \
                and not k.startswith(_ENV_NEVER):       # a README must not get the owner to set a loader variable
            names.setdefault(k)
    for p in mcp_check._files(root):
        if p.suffix.lower() not in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".go") or len(names) >= 30:
            continue
        if p.name.endswith("_test.go") or {"e2e", "test", "tests", "testdata", "script", "scripts"} & set(p.relative_to(root).parts[:-1]):
            continue                                    # what the tests and build scripts read is not what the server needs
        try:
            text = p.read_bytes()[:mcp_check.MAX_FILE_BYTES].decode("utf-8", "replace")
        except OSError:
            continue
        for m in _ENV_NAME.finditer(text):
            n = next(g for g in m.groups() if g)
            if n not in _ENV_IGNORE and not n.startswith(("npm_", "NPM_", "PYTHON", "UV_")) and not n.endswith("_") \
                    and n not in ("DISPLAY", "WAYLAND_DISPLAY", "GITHUB_ACTIONS"):
                names.setdefault(n)
    return list(names)[:30]


def _from_config(data: dict) -> dict:
    servers = data.get("mcpServers") or data.get("servers") if isinstance(data, dict) else None
    name, entry = "", data
    if isinstance(servers, dict) and servers:
        name, entry = next(iter(servers.items()))
    if not isinstance(entry, dict) or not (isinstance(entry.get("command"), str) or isinstance(entry.get("url"), str)):
        raise AdderError('the configuration needs a "command" (or a "url" for an http server)')
    args = entry.get("args") or []
    env = entry.get("env") or {}
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args) or not isinstance(env, dict):
        raise AdderError('"args" must be a list and "env" an object')
    http = not entry.get("command")
    return {"name": safe_name(name or (entry.get("command") or "server")), "transport": "http" if http else "stdio",
            "command": entry.get("command"), "args": [str(a) for a in args], "url": entry.get("url") if http else None,
            "cwd": None, "env": {}, "env_needed": [str(k) for k in env][:30], "install": [],
            "notes": ["Taken from the pasted configuration. No code was fetched or checked."]}


def propose(src: dict, root: Path | None, facts: dict) -> dict:
    """How this server would be installed and started. Nothing here runs."""
    kind = src["kind"]
    if kind == "config":
        return _from_config(src["data"])
    readme = _readme_config(root)
    notes: list[str] = []
    base = {"transport": "stdio", "url": None, "cwd": None, "env": {}, "env_needed": env_names(root, readme), "install": []}
    if readme:
        notes.append("The README's own start command: " + " ".join([readme["command"], *map(str, readme.get("args") or [])])[:300])
    if kind == "npm":
        return {**base, "name": safe_name(src["name"]), "command": "npx", "args": ["-y", f"{src['name']}@{facts['version']}"],
                "env": {"npm_config_ignore_scripts": "true"},
                "notes": [*notes, "npx downloads the package and its dependencies when the server starts. Install scripts are switched off."]}
    if kind == "pypi":
        scripts = _py_scripts(root)
        spec = f"{src['name']}=={facts['version']}"
        args = [spec] if not scripts or src["name"] in scripts else ["--from", spec, _start_script(scripts)]
        return {**base, "name": safe_name(src["name"]), "command": "uvx", "args": args,
                "notes": [*notes, "uvx downloads the package and its dependencies when the server starts."]}
    label = facts.get("label") or root.name
    if (root / "package.json").is_file():
        try:
            pkg = json.loads((root / "package.json").read_text(errors="replace"))
        except ValueError:
            raise AdderError("package.json in this code is not valid JSON") from None
        if pkg.get("workspaces"):
            raise AdderError("this repository holds several packages. Add the one you want by its package name "
                             "(npm:<name> or pypi:<name>), or give the folder of that one package")
        bins = pkg.get("bin")
        entry = bins if isinstance(bins, str) else (next(iter(bins.values()), None) if isinstance(bins, dict) and bins else None)
        entry = entry or pkg.get("main") or "index.js"
        if not isinstance(entry, str) or archives.safe_member(entry) is None:
            raise AdderError("package.json names a start file outside the package")
        install = [["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"]]
        scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
        runs = [None]
        if "build" in scripts and not (root / entry).is_file():
            install.append(["npm", "run", "build"])
            runs.append(f'{RUNS_CODE} Its "build" script is: {str(scripts["build"])[:600]}')
        if (root / entry).is_symlink():
            raise AdderError("package.json names a start file that is a link")
        return {**base, "name": safe_name(pkg.get("name") or label), "command": "node", "args": [str(root / entry)],
                "cwd": str(root), "install": install, "install_runs_code": runs, "notes": notes}
    if (root / "pyproject.toml").is_file():
        scripts = _py_scripts(root)
        if scripts:
            run = [_start_script(scripts)]
        else:
            main = next((n for n in ("server.py", "main.py", "app.py") if (root / n).is_file()), None)
            if main is None:
                raise AdderError("no start command was found in pyproject.toml and there is no server.py or main.py")
            run = ["python", main]
        return {**base, "name": safe_name(label), "command": "uv", "args": ["run", "--directory", str(root), *run],
                "cwd": str(root), "install": [["uv", "sync", "--directory", str(root)]],
                "install_runs_code": [f"{RUNS_CODE} uv builds this project and any dependency that has no ready-made wheel."],
                "notes": notes}
    for main in ("server.py", "main.py"):
        if (root / main).is_file():
            return {**base, "name": safe_name(label), "command": "uv", "args": ["run", "--directory", str(root), "python", main],
                    "cwd": str(root), "notes": [*notes, "A single Python file; uv supplies Python and any inline dependencies."]}
    hosted = _hosted(root)
    if (root / "go.mod").is_file():
        go = _go_proposal(root, label, base, notes)
        if go is not None:
            if hosted:
                go["notes"].append(f"The README also names a hosted server, {hosted[0]}. To use that and build nothing here, "
                                   'choose "Add by hand" with that address'
                                   + (f" and the header {', '.join(hosted[1])}." if hosted[1] else "."))
            return go
    if hosted:
        owner = urlsplit(hosted[0]).hostname
        return {**base, "name": safe_name(label.rstrip("/").rsplit("/", 1)[-1]), "transport": "http", "command": None, "args": [],
                "url": hosted[0], "env_needed": [],
                "notes": ["This repository holds no program HunterAI can start; its README names a hosted server instead. That "
                          f"server is run by whoever owns {owner}: no code from here runs on this machine, and what the tools "
                          "send goes to them.",
                          *([f"It asks for the header {', '.join(hosted[1])}: after adding the server, open Edit and fill it in."]
                            if hosted[1] else [])]}
    raise AdderError("no package.json, pyproject.toml, go.mod with a program, server.py, or main.py was found, and the README "
                     "names no hosted server: add this server by hand")


def _start_script(scripts) -> str:
    """The script that starts the server: one that says so in its name (cloud-audit-mcp next to
    cloud-audit, the command line program), else the first by name."""
    names = sorted(scripts)
    return next((n for n in names if n.lower().endswith(("-mcp", "_mcp", "-server", "_server"))),
                next((n for n in names if "mcp" in n.lower()), names[0]))


def _py_scripts(root: Path) -> dict:
    for ep in sorted(root.glob("*.dist-info/entry_points.txt")):       # a wheel
        try:
            section = re.search(r"\[console_scripts\]\s*\n(.*?)(?:\n\[|\Z)", ep.read_text(errors="replace"), re.S)
        except OSError:
            continue
        if section:
            return {m.group(1): m.group(2) for m in re.finditer(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]{0,79})\s*=\s*(\S+)", section.group(1), re.M)}
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(errors="replace"))
        scripts = (data.get("project") or {}).get("scripts") or {}
        return {k: v for k, v in scripts.items() if isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", k)}
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return {}


# ---- inspections

def _record(iid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", iid or ""):
        raise KeyError(iid)
    return _dir("inspections") / f"{iid}.json"


def load(iid: str) -> dict:
    f = _record(iid)
    if not f.is_file():
        raise KeyError(iid)
    return json.loads(f.read_text())


def _save(record: dict) -> None:
    f = _record(record["id"])
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(json.dumps(record))


async def inspect(source: str, *, client: httpx.AsyncClient | None = None, audit: bool = True) -> dict:
    """Fetch, read, and report. The result is what the owner approves (or does not)."""
    src = classify(source)
    iid = new_id()
    work = _dir("src", iid)
    try:
        root, facts = await fetch(src, work, client)
        proposal = propose(src, root, facts)
        report = mcp_check.scan(facts.get("scan_root") or root) if root is not None else None
        if report is not None and facts.get("unpack_problems"):
            report["truncated"] = True
            report["not_unpacked"] = facts["unpack_problems"]
        audit_result = await mcp_check.audit(mcp_check.pinned(root)) if (root is not None and audit) else None
    except (AdderError, limits.NoSpace) as e:
        shutil.rmtree(work, ignore_errors=True)
        raise AdderError(str(e)) from None
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    verdict = mcp_check.verdict(report, audit_result) if report is not None else {
        "level": "unchecked", "text": "No code was checked: this is a configuration you pasted. Starting it runs whatever its command names."}
    if src["kind"] in ("config", "folder"):
        shutil.rmtree(work, ignore_errors=True)
    record = {"id": iid, "source": source.strip()[:2000] if src["kind"] != "config" else "(pasted configuration)",
              "kind": src["kind"], "label": facts.get("label"), "root": str(root) if root else None,
              "proposal": proposal, "report": report, "audit": audit_result, "verdict": verdict, "created_at": now(),
              "digest": tree_digest(root) if root is not None else None}
    _save(record)
    return record


def tree_digest(root: Path) -> str:
    """A fingerprint of the code as it was inspected: every file's path and content, links by
    their target. Installing checks it again, so what is installed is what was read."""
    import hashlib
    h = hashlib.sha256()
    for dirpath, dirnames, names in os.walk(root, followlinks=False):
        dirnames.sort()
        for n in sorted(names):
            p = Path(dirpath) / n
            h.update(p.relative_to(root).as_posix().encode() + b"\0")
            try:
                if p.is_symlink():
                    h.update(b"link:" + os.readlink(p).encode())
                else:
                    with open(p, "rb") as f:
                        for block in iter(lambda: f.read(1024 * 1024), b""):
                            h.update(block)
            except OSError:
                h.update(b"unreadable")
            h.update(b"\0")
    return h.hexdigest()


def _bare_env(extra: dict | None = None) -> dict:
    """What an install step sees: a path and a home, none of HunterAI's keys."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"), "LANG": "C.UTF-8",
            # Set here, these win over an .npmrc that came with the code.
            "npm_config_ignore_scripts": "true", "npm_config_git": "git", "npm_config_fund": "false",
            "npm_config_update_notifier": "false", "npm_config_userconfig": "/dev/null", "npm_config_globalconfig": "/dev/null",
            "CI": "1",
            # go build: no C compiler run on the module's say-so, no newer toolchain fetched and run, go.mod left as it is.
            "CGO_ENABLED": "0", "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly", **(extra or {})}


async def install(iid: str, *, name: str, risk: str, env: dict, approved_install: list, command: str | None = None,
                  args: list | None = None) -> mcp_manager.ServerConfig:
    """Run the install steps that were shown, then write the server into mcp.json, switched off.
    `approved_install` must be exactly the steps of the inspection: what was approved is what runs."""
    record = load(iid)
    proposal = record["proposal"]
    if approved_install != proposal["install"]:
        raise AdderError("the install steps sent do not match the inspected ones; inspect again")
    if name in mcp_manager.load_config()[0]:
        raise AdderError(f'a server named "{name}" already exists')
    fields = {"transport": proposal["transport"], "command": command or proposal["command"],
              "args": [str(a) for a in (args if args is not None else proposal["args"])], "url": proposal.get("url"),
              "cwd": proposal.get("cwd"), "risk": risk, "enabled": False,
              "env": {**proposal.get("env", {}), **{k: v for k, v in (env or {}).items() if v not in (None, "")}}}
    try:   # validate before anything runs
        mcp_manager.ServerConfig(**{**{k: v for k, v in fields.items() if v is not None}, "name": name})
    except ValueError as e:
        raise AdderError(mcp_manager.problem(e)) from None
    root = Path(record["root"]) if record.get("root") else None
    if root is not None and record.get("digest") and (not root.is_dir() or tree_digest(root) != record["digest"]):
        raise AdderError("the code changed since it was inspected; inspect it again")
    for step in proposal["install"]:
        if root is None or not root.is_dir():
            raise AdderError("the inspected code is no longer on disk; inspect again")
        try:
            extra = {k: str(v) for k, v in (proposal.get("install_env") or {}).items() if k == "GOTOOLCHAIN"}
            res = await tools.run_tool([str(a) for a in step], timeout=INSTALL_TIMEOUT, cwd=root, env=_bare_env(extra))
        except tools.ToolMissing:
            raise AdderError(f"{step[0]} is not installed on this machine") from None
        except tools.ToolTimeout:
            raise AdderError(f"{' '.join(step[:3])} timed out after {INSTALL_TIMEOUT:g} s") from None
        except tools.ToolLimit:
            raise AdderError(f"{' '.join(step[:3])} produced too much output") from None
        if res.returncode != 0:
            tail = (res.stderr or res.stdout).decode("utf-8", "replace").strip()[-800:]
            raise AdderError(f"{' '.join(step[:3])} failed (exit {res.returncode}):\n{tail}")
    try:
        return mcp_manager.put_server(name, {k: v for k, v in fields.items() if v is not None}, create=True)
    except FileExistsError:
        raise AdderError(f'a server named "{name}" already exists') from None
    except ValueError as e:
        raise AdderError(str(e)) from None


HINTS = (
    (r"(?i)(command not found|ENOENT|No such file or directory|not recognized as)", "The program this server starts with was not found. Install its runtime (Node.js for npx/node, uv for uvx/uv) or correct the command."),
    (r"(?i)(Cannot find module|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|No module named|ImportError)", "A dependency is missing. Run the install step again, or for an npm project the build step."),
    (r"(?i)(api[_ -]?key|token|credential|unauthori[sz]ed|401|403|missing required env|environment variable)", "It may be missing a key or setting. Check the environment values this server needs."),
    (r"(?i)(EADDRINUSE|address already in use)", "A port it wants is taken by another program."),
    (r"(?i)(SyntaxError|Unexpected token|unsupported engine|requires Node|requires-python)", "The installed Node.js or Python may be too old or too new for this server."),
    (r"(?i)(ETIMEDOUT|ECONNREFUSED|getaddrinfo|network is unreachable|could not resolve)", "It could not reach the network while starting."),
)


def log_tail(name: str, chars: int = 3000) -> str:
    f = config.logs_dir() / f"mcp-{name}.log"
    try:
        with open(f, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - chars))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def hints(text: str) -> list[str]:
    return [h for rx, h in HINTS if re.search(rx, text or "")]
