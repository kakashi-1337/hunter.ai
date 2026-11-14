import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from hunterai import config, mcp_adder, mcp_check, mcp_manager
from hunterai.mcp_adder import AdderError
from tests.kb_samples import tar_bytes, tree, zip_bytes
from tests.test_api import client  # noqa: F401

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))
README = '''# Demo

```json
{"mcpServers": {"demo": {"command": "npx", "args": ["-y", "demo-mcp"], "env": {"DEMO_API_KEY": "<your key>"}}}}
```
'''


@pytest.mark.parametrize("source,want", [
    ("https://github.com/owner/repo", {"kind": "git", "url": "https://github.com/owner/repo.git", "label": "github.com/owner/repo"}),
    ("npm:@scope/server-x@1.2.3", {"kind": "npm", "name": "@scope/server-x", "version": "1.2.3"}),
    ("@scope/server-x", {"kind": "npm", "name": "@scope/server-x", "version": None}),
    ("https://www.npmjs.com/package/demo-mcp", {"kind": "npm", "name": "demo-mcp", "version": None}),
    ("pypi:mcp-server-fetch==0.6.2", {"kind": "pypi", "name": "mcp-server-fetch", "version": "0.6.2"}),
    ("https://pypi.org/project/mcp-server-fetch/", {"kind": "pypi", "name": "mcp-server-fetch", "version": None}),
])
def test_classify(source, want):
    assert mcp_adder.classify(source) == want


@pytest.mark.parametrize("source", ["", "demo-mcp", "https://evil.example/x/y", "npm:../../x", "npm:--flag", "pypi:a b", "{not json", "relative/folder",
                                    "file:///etc", "npm:x@1 && id"])
def test_classify_refuses(source):
    with pytest.raises(AdderError):
        mcp_adder.classify(source)


def test_classify_folder_and_config(tmp_path):
    tree(tmp_path / "srv", {"server.py": b"x"})
    assert mcp_adder.classify(str(tmp_path / "srv")) == {"kind": "folder", "path": str((tmp_path / "srv").resolve())}
    config.home().mkdir(parents=True, exist_ok=True)
    for bad in (str(config.home()), str(tmp_path / "missing")):
        with pytest.raises(AdderError):
            mcp_adder.classify(bad)
    assert mcp_adder.classify('{"command": "x"}')["kind"] == "config"


@pytest.mark.parametrize("raw,name", [("@modelcontextprotocol/server-filesystem", "filesystem"), ("mcp-server-fetch", "fetch"),
                                      ("github.com/o/My.Cool-MCP", "my_cool"), ("123", "s_123"), ("x" * 60, "x" * 24)])
def test_safe_name(raw, name):
    assert mcp_adder.safe_name(raw) == name


def test_proposals_for_node_and_python_projects(tmp_path):
    node = tmp_path / "node"
    tree(node, {"package.json": json.dumps({"name": "demo-mcp", "bin": {"demo": "dist/index.js"}, "scripts": {"build": "tsc"}}).encode(),
                "src/index.ts": b"const k = process.env.DEMO_API_KEY; const h = process.env.HOME; const t = process.env['OTHER_TOKEN']",
                "README.md": README.encode()})
    p = mcp_adder.propose({"kind": "folder", "path": str(node)}, node, {"label": str(node)})
    assert (p["name"], p["command"], p["args"], p["cwd"]) == ("demo", "node", [str(node / "dist/index.js")], str(node))
    assert p["install"] == [["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"], ["npm", "run", "build"]]
    assert p["install_runs_code"][0] is None and p["install_runs_code"][1].startswith(mcp_adder.RUNS_CODE) and "tsc" in p["install_runs_code"][1]
    assert p["env_needed"] == ["DEMO_API_KEY", "OTHER_TOKEN"] and any("README's own start command: npx -y demo-mcp" in n for n in p["notes"])
    py = tmp_path / "py"
    tree(py, {"pyproject.toml": b'[project]\nname = "fetcher"\n[project.scripts]\nfetch-server = "fetcher:main"\n',
              "fetcher.py": b'import os\nKEY = os.environ["FETCH_KEY"]\nX = os.getenv("PROXY_URL")\n'})
    p = mcp_adder.propose({"kind": "folder", "path": str(py)}, py, {"label": "github.com/o/fetcher-mcp"})
    assert (p["name"], p["command"], p["args"]) == ("fetcher", "uv", ["run", "--directory", str(py), "fetch-server"])
    assert p["install"] == [["uv", "sync", "--directory", str(py)]] and p["env_needed"] == ["FETCH_KEY", "PROXY_URL"]
    assert p["install_runs_code"][0].startswith(mcp_adder.RUNS_CODE)
    with pytest.raises(AdderError, match="add this server by hand"):
        mcp_adder.propose({"kind": "folder", "path": str(tmp_path)}, tmp_path / "py" / "..", {})
    tree(tmp_path / "bad", {"package.json": b'{"name": "x", "bin": "../../../usr/bin/evil"}'})
    with pytest.raises(AdderError, match="outside the package"):
        mcp_adder.propose({"kind": "folder", "path": "x"}, tmp_path / "bad", {})


def test_pasted_configuration():
    p = mcp_adder.propose(mcp_adder.classify(json.dumps({"mcpServers": {"Brave Search": {"command": "npx", "args": ["-y", "brave"], "env": {"BRAVE_KEY": "abc123"}}}})), None, {})
    assert (p["name"], p["command"], p["args"], p["env_needed"], p["env"], p["install"]) == ("brave_search", "npx", ["-y", "brave"], ["BRAVE_KEY"], {}, [])
    assert "abc123" not in json.dumps(p)                                   # a pasted key value is not kept
    http = mcp_adder.propose(mcp_adder.classify('{"url": "http://127.0.0.1:9000/mcp"}'), None, {})
    assert (http["transport"], http["url"], http["command"]) == ("http", "http://127.0.0.1:9000/mcp", None)
    with pytest.raises(AdderError):
        mcp_adder.propose(mcp_adder.classify('{"args": []}'), None, {})


def npm_tarball(files):
    return tar_bytes([(tarfile.TarInfo(f"package/{n}"), d) for n, d in files.items()], mode="w:gz")


@respx.mock
async def test_inspect_an_npm_package_fetches_checks_and_proposes_without_running(tmp_path):
    tgz = npm_tarball({"package.json": json.dumps({"name": "demo-mcp", "version": "1.2.3", "scripts": {"postinstall": "node x.js"}}).encode(),
                       "x.js": b"require('child_process').execSync('id')\n",
                       "package-lock.json": json.dumps({"packages": {"node_modules/lodash": {"version": "4.17.20"}}}).encode()})
    respx.get("https://registry.npmjs.org/demo-mcp").mock(return_value=httpx.Response(200, json={
        "dist-tags": {"latest": "1.2.3"}, "versions": {"1.2.3": {"dist": {"tarball": "https://registry.npmjs.org/demo-mcp/-/demo-mcp-1.2.3.tgz"}}}}))
    respx.get("https://registry.npmjs.org/demo-mcp/-/demo-mcp-1.2.3.tgz").mock(return_value=httpx.Response(200, content=tgz))
    respx.post(mcp_check.OSV_URL).mock(return_value=httpx.Response(200, json={"results": [{"vulns": [{"id": "GHSA-x"}]}]}))
    rec = await mcp_adder.inspect("npm:demo-mcp")
    p = rec["proposal"]
    assert (rec["kind"], rec["label"], p["command"], p["args"], p["install"]) == ("npm", "demo-mcp@1.2.3", "npx", ["-y", "demo-mcp@1.2.3"], [])
    assert p["env"] == {"npm_config_ignore_scripts": "true"}
    assert {f["rule"] for f in rec["report"]["findings"]} == {"install-script", "process-spawn"}
    assert rec["audit"]["vulnerable"][0]["ids"] == ["GHSA-x"] and rec["verdict"]["level"] == "caution"
    assert mcp_adder.load(rec["id"]) == rec and (Path(rec["root"]) / "x.js").is_file()
    assert (config.home() / "mcp" / "inspections" / f"{rec['id']}.json").stat().st_mode & 0o777 == 0o600


@respx.mock
async def test_registry_problems_are_plain_errors_and_leave_nothing():
    respx.get("https://registry.npmjs.org/nope").mock(return_value=httpx.Response(404))
    with pytest.raises(AdderError, match="no such package"):
        await mcp_adder.inspect("npm:nope")
    respx.get("https://registry.npmjs.org/redir").mock(return_value=httpx.Response(200, json={
        "dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {"dist": {"tarball": "https://evil.example/x.tgz"}}}}))
    with pytest.raises(AdderError, match="unexpected download host"):
        await mcp_adder.inspect("npm:redir")
    respx.get("https://pypi.org/pypi/pkg/json").mock(return_value=httpx.Response(200, json={"info": {"version": "1.0"}, "urls": []}))
    with pytest.raises(AdderError, match="no source archive"):
        await mcp_adder.inspect("pypi:pkg")
    assert not [p for p in (config.home() / "mcp" / "src").iterdir()]


@respx.mock
async def test_inspect_a_pypi_wheel_uses_its_script_name():
    whl = zip_bytes({"pkg/__init__.py": b"import os\nos.environ['PKG_TOKEN']\n", "pkg-2.0.dist-info/entry_points.txt": b"[console_scripts]\nrun-pkg = pkg:main\n\n[other]\nx = y\n"})
    respx.get("https://pypi.org/pypi/pkg/2.0/json").mock(return_value=httpx.Response(200, json={
        "info": {"version": "2.0"}, "urls": [{"packagetype": "bdist_wheel", "filename": "pkg-2.0-py3-none-any.whl",
                                                 "url": "https://files.pythonhosted.org/packages/pkg-2.0-py3-none-any.whl"}]}))
    respx.get("https://files.pythonhosted.org/packages/pkg-2.0-py3-none-any.whl").mock(return_value=httpx.Response(200, content=whl))
    respx.post(mcp_check.OSV_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    rec = await mcp_adder.inspect("pypi:pkg==2.0")
    assert (rec["proposal"]["command"], rec["proposal"]["args"], rec["proposal"]["env_needed"]) == ("uvx", ["--from", "pkg==2.0", "run-pkg"], ["PKG_TOKEN"])


async def folder_inspection(tmp_path):
    srv = tmp_path / "srv"
    tree(srv, {"package.json": json.dumps({"name": "local-mcp", "main": "index.js"}).encode(), "index.js": b"console.log(process.env.LOCAL_KEY)"})
    return srv, await mcp_adder.inspect(str(srv), audit=False)


async def test_install_runs_only_the_approved_steps_with_no_keys_and_adds_the_server_switched_off(tmp_path, fake_bin, monkeypatch):
    monkeypatch.setenv("ABLIT_API_KEY", "ak_secret")
    srv, rec = await folder_inspection(tmp_path)
    fake_bin("npm", f'#!/bin/sh\necho "$@" >> "{tmp_path}/npm.args"\nenv > "{tmp_path}/npm.env"\n')
    steps = rec["proposal"]["install"]
    with pytest.raises(AdderError, match="do not match"):
        await mcp_adder.install(rec["id"], name="local", risk="execute", env={}, approved_install=[["npm", "install"]])
    assert not (tmp_path / "npm.args").exists()
    cfg = await mcp_adder.install(rec["id"], name="local", risk="execute", env={"LOCAL_KEY": "v1", "EMPTY": ""}, approved_install=steps)
    assert (tmp_path / "npm.args").read_text().split() == ["install", "--ignore-scripts", "--no-audit", "--no-fund"]
    env = (tmp_path / "npm.env").read_text()
    assert "ak_secret" not in env and "npm_config_ignore_scripts=true" in env and "npm_config_git=git" in env
    assert (cfg.enabled, cfg.command, cfg.args, cfg.risk, cfg.env, cfg.cwd) == (False, "node", [str(srv / "index.js")], "execute", {"LOCAL_KEY": "v1"}, str(srv))
    stored = json.loads(config.mcp_file().read_text())["servers"]["local"]
    assert stored["enabled"] is False and config.mcp_file().stat().st_mode & 0o777 == 0o600
    with pytest.raises(AdderError, match="already exists"):
        await mcp_adder.install(rec["id"], name="local", risk="execute", env={}, approved_install=steps)
    for bad in ("chunker", "Bad Name"):
        with pytest.raises(AdderError):
            await mcp_adder.install(rec["id"], name=bad, risk="execute", env={}, approved_install=steps)


async def test_a_failing_install_step_adds_nothing_and_shows_its_output(tmp_path, fake_bin):
    _, rec = await folder_inspection(tmp_path)
    fake_bin("npm", "#!/bin/sh\necho 'npm ERR! code E404' >&2\nexit 1\n")
    with pytest.raises(AdderError, match=r"npm install --ignore-scripts failed \(exit 1\):\nnpm ERR! code E404"):
        await mcp_adder.install(rec["id"], name="local", risk="execute", env={}, approved_install=rec["proposal"]["install"])
    assert "local" not in mcp_manager.load_config()[0]


def test_put_and_remove_server_entries(isolated_home):
    cfg = mcp_manager.put_server("scan", {"command": "scan-mcp", "args": ["--x"], "env": {"TOKEN": "t1", "MODE": "a"}, "risk": "network"}, create=True)
    assert cfg.env == {"TOKEN": "t1", "MODE": "a"}
    kept = mcp_manager.put_server("scan", {"env": {"TOKEN": None, "MODE": "b"}, "args": []}, create=False)
    assert kept.env == {"TOKEN": "t1", "MODE": "b"} and kept.args == [] and kept.command == "scan-mcp"
    with pytest.raises(FileExistsError):
        mcp_manager.put_server("scan", {"command": "x"}, create=True)
    with pytest.raises(KeyError):
        mcp_manager.put_server("nope", {"command": "x"}, create=False)
    for name in ("chunker", "rea"):
        with pytest.raises(ValueError):
            mcp_manager.put_server(name, {"command": "evil"}, create=False)
        with pytest.raises(ValueError):
            mcp_manager.remove_server(name)
    with pytest.raises(ValueError):
        mcp_manager.put_server("bad", {"transport": "stdio"}, create=True)
    mcp_manager.remove_server("scan")
    assert "scan" not in mcp_manager.load_config()[0]


def test_hints_from_what_a_server_printed():
    assert "runtime" in mcp_adder.hints("sh: 1: npx: command not found")[0]
    assert "dependency" in mcp_adder.hints("Error: Cannot find module 'zod'")[0]
    assert any("key" in h for h in mcp_adder.hints("Error: BRAVE_API_KEY environment variable is required"))
    assert mcp_adder.hints("all good") == []


async def test_api_flow_inspect_install_enable_check_edit_delete(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTERAI_MCP", "on")
    manager = mcp_manager.Manager({}); monkeypatch.setattr(mcp_manager, "manager", manager)
    try:
        pasted = json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [FAKE], "env": {"SECRET_TOKEN": "x"}}}})
        rec = (await client.post("/api/mcp/inspect", json={"source": pasted})).json()
        assert rec["verdict"]["level"] == "unchecked" and rec["proposal"]["env_needed"] == ["SECRET_TOKEN"] and rec["report"] is None
        assert (await client.post("/api/mcp/inspect", json={"source": "what"})).status_code == 400
        assert (await client.get("/api/mcp/inspections/" + "0" * 32)).status_code == 404
        r = await client.post(f"/api/mcp/inspections/{rec['id']}/install", json={"name": "fake", "risk": "read", "env": {"SECRET_TOKEN": "tok_123"}, "approved_install": []})
        assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["env_names"] == ["SECRET_TOKEN"] and "tok_123" not in r.text
        listed = (await client.get("/api/mcp/servers")).json()
        assert "tok_123" not in json.dumps(listed) and {s["name"] for s in listed["servers"]} >= {"fake", "chunker", "rea"}
        off = (await client.post("/api/mcp/servers/fake/check")).json()
        assert off["state"] == "disabled" and off["ping_ms"] is None
        assert (await client.put("/api/mcp/fake", json={"enabled": True})).status_code == 200
        ok = (await client.post("/api/mcp/servers/fake/check")).json()
        assert ok["state"] == "ready" and "fake_echo" in ok["tools"] and ok["ping_ms"] is not None and ok["hints"] == []
        changed = (await client.patch("/api/mcp/servers/fake", json={"args": [FAKE, "--nope"], "env": {"SECRET_TOKEN": None, "EXTRA": "1"}})).json()
        assert changed["env_names"] == ["EXTRA", "SECRET_TOKEN"] and json.loads(config.mcp_file().read_text())["servers"]["fake"]["env"]["SECRET_TOKEN"] == "tok_123"
        assert (await client.patch("/api/mcp/servers/chunker", json={"command": "evil"})).status_code == 400
        assert (await client.post("/api/mcp/servers", json={"name": "fake", "command": "x"})).status_code == 409
        assert (await client.post("/api/mcp/servers", json={"name": "byhand", "command": "definitely-not-a-program-xyz", "risk": "network"})).json()["enabled"] is False
        await client.put("/api/mcp/byhand", json={"enabled": True})
        dead = (await client.post("/api/mcp/servers/byhand/check")).json()
        assert dead["state"] != "ready" and dead["ping_ms"] is None
        assert (await client.delete("/api/mcp/servers/fake")).status_code == 204 and (await client.delete("/api/mcp/servers/fake")).status_code == 404
        assert (await client.delete("/api/mcp/servers/rea")).status_code == 400
        from hunterai.tools import registry
        assert registry.get("fake_echo") is None
    finally:
        await manager.stop()


async def test_model_review_is_advice_stored_with_the_inspection(client, conn, tmp_path):
    from hunterai.providers import registry as providers
    from hunterai.repo import settings
    from tests.test_decision_engine import Json
    tree(tmp_path / "srv", {"server.py": b"import os\nopen(os.path.expanduser('~/.ssh/id_rsa')).read()\n"})
    rec = await mcp_adder.inspect(str(tmp_path / "srv"), audit=False)
    await settings.put(conn, "decision_provider", "decider"); await settings.put(conn, "decision_model", "d")
    d = Json("decider", {"malicious": 0.9, "advice": {"install": 0.02, "read_first": 0.08, "do_not_install": 0.9}}, "garbage", "garbage")
    providers.override("decider", d)
    got = (await client.post(f"/api/mcp/inspections/{rec['id']}/review")).json()
    assert (got["advice"], got["invalid"]) == ("do_not_install", False) and got["malicious_probability"] == 0.9
    sent = d.calls[0]["messages"][0].blocks[0].text
    assert "credential-read" not in sent and "Reads keys, credentials" in sent and "server.py:2" in sent
    assert mcp_adder.load(rec["id"])["review"]["advice"] == "do_not_install"
    bad = (await client.post(f"/api/mcp/inspections/{rec['id']}/review")).json()
    assert bad["invalid"] is True and bad["advice"] == "read_first"            # an unusable answer never reads as "install"


def test_a_repository_of_several_packages_is_not_guessed_at(tmp_path):
    tree(tmp_path / "mono", {"package.json": b'{"name": "servers", "workspaces": ["src/*"]}'})
    with pytest.raises(AdderError, match="several packages"):
        mcp_adder.propose({"kind": "git"}, tmp_path / "mono", {"label": "github.com/o/servers"})


@respx.mock
async def test_a_package_with_archives_inside_is_checked_whole_and_rooted_at_itself():
    inner = zip_bytes({"payload.js": b"eval(atob('eA=='))"})
    tgz = npm_tarball({"package.json": b'{"name": "nest", "version": "1.0.0"}', "index.js": b"console.log(1)", "a.zip": inner, "b.zip": zip_bytes({"readme.txt": b"hi"})})
    respx.get("https://registry.npmjs.org/nest").mock(return_value=httpx.Response(200, json={
        "dist-tags": {"latest": "1.0.0"}, "versions": {"1.0.0": {"dist": {"tarball": "https://registry.npmjs.org/nest/-/nest-1.0.0.tgz"}}}}))
    respx.get("https://registry.npmjs.org/nest/-/nest-1.0.0.tgz").mock(return_value=httpx.Response(200, content=tgz))
    rec = await mcp_adder.inspect("npm:nest", audit=False)
    assert (Path(rec["root"]) / "package.json").is_file() and (Path(rec["root"]) / "index.js").is_file()      # never a nested archive's folder
    assert any(f["rule"] == "encoded-exec" and f["file"].endswith("payload.js") for f in rec["report"]["findings"])
    assert rec["verdict"]["level"] == "danger"


async def test_code_changed_after_the_inspection_is_not_installed(tmp_path, fake_bin):
    srv, rec = await folder_inspection(tmp_path)
    fake_bin("npm", "#!/bin/sh\nexit 0\n")
    (srv / "index.js").write_text("require('child_process').execSync('id')")
    with pytest.raises(AdderError, match="changed since it was inspected"):
        await mcp_adder.install(rec["id"], name="local", risk="execute", env={}, approved_install=rec["proposal"]["install"])
    assert "local" not in mcp_manager.load_config()[0]


def test_readme_cannot_suggest_loader_variables_and_script_names_cannot_be_options(tmp_path):
    readme = '```json\n{"mcpServers": {"x": {"command": "npx", "env": {"NODE_OPTIONS": "--require ./x", "LD_PRELOAD": "x", "PATH": "/tmp", "REAL_KEY": "k"}}}}\n```\n'
    tree(tmp_path / "s", {"README.md": readme.encode(), "pyproject.toml": b'[project]\nname="s"\n[project.scripts]\n"--with-editable" = "x:y"\nok-run = "x:y"\n'})
    p = mcp_adder.propose({"kind": "folder"}, tmp_path / "s", {"label": "s"})
    assert p["env_needed"] == ["REAL_KEY"] and p["args"][-1] == "ok-run"


def test_installed_code_and_inspection_records_are_protected_from_tools(isolated_home):
    from hunterai.tools import resolve
    base = config.home() / "mcp"
    for path in (base / "src" / "abc" / "code" / "index.js", base / "inspections" / ("0" * 32 + ".json")):
        assert "owner only" in resolve.protected(path, "write")
    assert resolve.protected(base / "src" / "abc" / "code" / "index.js", "read") is None


def test_validation_problems_name_the_field_not_the_value():
    with pytest.raises(ValueError) as e:
        mcp_manager.put_server("Bad Name", {"command": "secret-token-value"}, create=True)
    assert "name" in str(e.value) and "lowercase" in str(e.value) and "secret-token-value" not in str(e.value) and "pydantic" not in str(e.value)


GO_README = '''# Acme MCP server

```bash
go install example.test/acme@latest
```

## Remote

```json
{"servers": {"acme": {"type": "http", "url": "https://mcp.acme.test/mcp/", "headers": {"Authorization": "Bearer ${input:acme_pat}"}}}}
```

## Docker

```json
{"mcpServers": {"acme": {"command": "docker", "args": ["run", "-i", "--rm", "-e", "ACME_TOKEN", "ghcr.io/acme/acme-mcp-server"], "env": {"ACME_TOKEN": "<token>"}}}}
```

## From source

```JSON
{"mcp": {"servers": {"acme": {"command": "/path/to/acme-mcp-server", "args": ["stdio", "--read-only"], "env": {"ACME_TOKEN": "<token>"}}}}}
```

## Enterprise

```json
"acme": {
    "command": "docker",
    "args": ["run", "ghcr.io/acme/acme-mcp-server"],
    "env": {"ACME_ENTERPRISE_HOST": "https://<your host>"}
}
```
'''
GO_MAIN = b'package main\n\nimport "os"\n\nfunc main() { _ = os.Getenv("ACME_HOST"); _, _ = os.LookupEnv("ACME_DEBUG_LOG"); _ = os.Getenv("DISPLAY") }\n'


@pytest.fixture(autouse=True)
def go_on_this_machine(monkeypatch):
    monkeypatch.setattr(mcp_adder, "_go_version", lambda: (1, 26, 8))


def test_a_go_repository_is_built_with_go_and_started_as_its_readme_says(tmp_path):
    root = tmp_path / "code"
    tree(root, {"go.mod": b"module github.com/acme/acme-mcp-server\n\ngo 1.24\n", "cmd/acme-mcp-server/main.go": GO_MAIN,
                "cmd/mcpcurl/main.go": b"package main\nfunc main() {}\n", "pkg/x/x.go": b"package x\n", "README.md": GO_README.encode(),
                "pkg/x/x_test.go": b'package x\nvar _ = os.Getenv("ACME_TEST_ONLY")\n', "e2e/run.go": b'package e2e\nvar _ = os.Getenv("ACME_E2E_TOKEN")\n'})
    p = mcp_adder.propose({"kind": "git"}, root, {"label": "github.com/acme/acme-mcp-server"})
    binary = str(root / ".hunterai-bin" / "acme-mcp-server")
    assert (p["name"], p["transport"], p["command"], p["args"], p["cwd"]) == ("acme", "stdio", binary, ["stdio", "--read-only"], str(root))
    assert p["install"] == [["go", "build", "-trimpath", "-p", "2", "-o", binary, "./cmd/acme-mcp-server"]]
    assert "go build" in p["install_runs_code"][0] and not p["install_runs_code"][0].startswith(mcp_adder.RUNS_CODE)
    assert p["env_needed"] == ["ACME_TOKEN", "ACME_ENTERPRISE_HOST", "ACME_HOST", "ACME_DEBUG_LOG"]
    assert any("https://mcp.acme.test/mcp/" in n and "Authorization" in n and "Add by hand" in n for n in p["notes"])


def test_go_programs_at_the_root_and_a_choice_that_cannot_be_made(tmp_path):
    one = tmp_path / "one"
    tree(one, {"go.mod": b"module example.test/tool\n", "main.go": b"// the server\npackage main\nfunc main() {}\n"})
    p = mcp_adder.propose({"kind": "folder", "path": str(one)}, one, {"label": "gitlab.com/o/tool-mcp"})
    assert p["install"][0][-1] == "." and p["command"] == str(one / ".hunterai-bin" / "tool-mcp") and p["args"] == []
    many = tmp_path / "many"
    tree(many, {"go.mod": b"module example.test/suite\n", "cmd/alpha/main.go": b"package main\n", "cmd/beta/main.go": b"package main\n"})
    with pytest.raises(AdderError, match="alpha, beta"):
        mcp_adder.propose({"kind": "folder", "path": str(many)}, many, {"label": "github.com/o/suite"})
    lib = tmp_path / "lib"
    tree(lib, {"go.mod": b"module example.test/lib\n", "lib.go": b"package lib\n"})
    with pytest.raises(AdderError, match="add this server by hand"):
        mcp_adder.propose({"kind": "folder", "path": str(lib)}, lib, {"label": "github.com/o/lib"})


def test_a_repository_that_only_names_a_hosted_server_is_added_as_that_address(tmp_path):
    root = tmp_path / "code"
    tree(root, {"README.md": GO_README.split("## Docker")[0].encode(), "docs/x.md": b"x"})
    p = mcp_adder.propose({"kind": "git"}, root, {"label": "github.com/acme/acme-mcp-server"})
    assert (p["transport"], p["url"], p["command"], p["install"], p["name"]) == ("http", "https://mcp.acme.test/mcp/", None, [], "acme")
    assert any("Authorization" in n and "Edit" in n for n in p["notes"]) and any("run by" in n for n in p["notes"])
    tree(tmp_path / "plain", {"README.md": b'```json\n{"servers": {"a": {"url": "http://mcp.acme.test/"}}}\n```\n'})
    tree(tmp_path / "pkg", {"README.md": b'```json\n{"name": "x", "repository": {"type": "git", "url": "https://github.com/o/r"}}\n```\n'})
    with pytest.raises(AdderError, match="add this server by hand"):            # a "url" that is no server entry
        mcp_adder.propose({"kind": "git"}, tmp_path / "pkg", {"label": "github.com/o/r"})
    with pytest.raises(AdderError, match="add this server by hand"):            # not https
        mcp_adder.propose({"kind": "git"}, tmp_path / "plain", {"label": "github.com/o/r"})


def test_go_dependencies_are_audited_and_the_build_runs_without_cgo_or_a_fetched_toolchain(tmp_path):
    tree(tmp_path, {"go.sum": b"github.com/acme/lib v1.2.3 h1:abc=\ngithub.com/acme/lib v1.2.3/go.mod h1:def=\n"
                              b"golang.org/x/net v0.30.0/go.mod h1:x=\ngopkg.in/old.v2 v2.0.0+incompatible h1:y=\n"})
    assert mcp_check.pinned(tmp_path) == [{"ecosystem": "Go", "name": "github.com/acme/lib", "version": "1.2.3"},
                                           {"ecosystem": "Go", "name": "gopkg.in/old.v2", "version": "2.0.0+incompatible"}]
    env = mcp_adder._bare_env()
    assert env["CGO_ENABLED"] == "0" and env["GOTOOLCHAIN"] == "local" and env["GOFLAGS"] == "-mod=readonly"


def test_a_module_that_asks_for_a_newer_go_gets_it_as_a_step_the_owner_sees(tmp_path, monkeypatch):
    root = tmp_path / "code"
    tree(root, {"go.mod": b"module example.test/tool\n\ngo 1.27.3\n", "main.go": b"package main\nfunc main() {}\n"})
    p = mcp_adder.propose({"kind": "git"}, root, {"label": "github.com/o/tool"})
    assert p["install_env"] == {"GOTOOLCHAIN": "go1.27.3"}
    assert "asks for Go 1.27.3 and this machine has 1.26.8" in p["install_runs_code"][0] and "downloads that toolchain" in p["install_runs_code"][0]
    (root / "go.mod").write_bytes(b"module example.test/tool\n\ngo 1.24\n\ntoolchain go1.28.0\n")           # the toolchain line wins
    assert mcp_adder.propose({"kind": "git"}, root, {"label": "github.com/o/tool"})["install_env"] == {"GOTOOLCHAIN": "go1.28.0"}
    for old in (b"go 1.24\n", b"go 1.26.8\n", b"go $(evil)\n", b""):
        (root / "go.mod").write_bytes(b"module example.test/tool\n\n" + old)
        p = mcp_adder.propose({"kind": "git"}, root, {"label": "github.com/o/tool"})
        assert p["install_env"] == {} and "toolchain" not in p["install_runs_code"][0]
    monkeypatch.setattr(mcp_adder, "_go_version", lambda: None)
    p = mcp_adder.propose({"kind": "git"}, root, {"label": "github.com/o/tool"})
    assert p["install_env"] == {} and any("go is not installed" in n for n in p["notes"])
    assert mcp_adder._version("go1.26.8") == (1, 26, 8) and mcp_adder._version("1.27") == (1, 27, 0) and mcp_adder._version("devel +abc") is None


async def test_the_newer_go_reaches_the_build_step_and_nothing_else_does(tmp_path, fake_bin, monkeypatch):
    out = fake_bin("go", '#!/bin/sh\necho "$GOTOOLCHAIN|$CGO_ENABLED|$EVIL" > "%s/go.env"\n' % tmp_path)
    root = tmp_path / "code"
    tree(root, {"go.mod": b"module example.test/tool\n\ngo 1.27.3\n", "main.go": b"package main\nfunc main() {}\n"})
    record = await mcp_adder.inspect(str(root), audit=False)
    record["proposal"]["install_env"]["EVIL"] = "1"                 # a record that was tampered with gains nothing
    mcp_adder._save(record)
    await mcp_adder.install(record["id"], name="tool", risk="execute", env={}, approved_install=record["proposal"]["install"])
    assert (tmp_path / "go.env").read_text().strip() == "go1.27.3|0|"


def test_the_script_named_for_the_server_is_the_start_command():
    from hunterai.mcp_adder import _start_script
    assert _start_script({"cloud-audit": "a:app", "cloud-audit-mcp": "a.mcp_server:main"}) == "cloud-audit-mcp"
    assert _start_script({"tool": "a:b", "tool-server": "a:c"}) == "tool-server"
    assert _start_script({"zeta": "a:b", "mcp-alpha-cli": "a:c"}) == "mcp-alpha-cli"
    assert _start_script({"beta": "a:b", "alpha": "a:c"}) == "alpha"
