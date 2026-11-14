import os
from pathlib import Path

import httpx
import pytest

from hunterai import config
from hunterai.tools import resolve
from hunterai.tools.base import HostUse, ToolError
from hunterai.types import Target

@pytest.mark.parametrize("url,host", [
    ("http://good.test@evil.test/", "evil.test"),
    ("http://good.test:80@evil.test:8080/x", "evil.test"),
    ("https://EXAMPLE.com./a", "example.com"),
    ("http://bücher.example/", "xn--bcher-kva.example"),
    ("http://2130706433/", "127.0.0.1"), ("http://0x7f.0.0.1/", "127.0.0.1"),
    ("http://0177.0.0.1/", "127.0.0.1"), ("http://[::ffff:127.0.0.1]/", "127.0.0.1"),
    ("http://[::1]:8790/", "::1"),
])
def test_host_of_url_is_the_real_host(url, host):
    assert resolve.host_of_url(url).host == host

def test_host_of_url_never_looks_up_a_name():                 # the autouse no_dns fixture would fail a lookup
    assert resolve.host_of_url("https://x.test/p") == HostUse("x.test", 443, ())
    assert resolve.host_of_url("http://0x7f.0.0.1:9000/") == HostUse("127.0.0.1", 9000, ("127.0.0.1",))

@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://a.test/", "javascript:alert(1)", "http:///nohost"])
def test_non_http_urls_rejected(url):
    with pytest.raises(ToolError): resolve.host_of_url(url)

@pytest.mark.parametrize("url,path", [("https://a.test/app/../admin", "/admin"), ("https://a.test/a/./b", "/a/b"),
                                      ("https://a.test", "/"), ("https://a.test/x?y=/../z", "/x")])
def test_url_path_is_the_path_that_is_sent(url, path):
    assert resolve.url_path(url) == path and httpx.URL(url).raw_path.split(b"?")[0] == path.encode()

@pytest.mark.parametrize("url", ["https://a.test/app/%2e%2e/admin", "https://a.test/app%2F..%2Fadmin",
                                 "https://a.test/app%5c..%5cadmin", "https://a.test/app\\..\\admin"])
def test_encoded_dots_and_slashes_in_a_path_are_refused(url):
    with pytest.raises(ToolError): resolve.url_path(url)

def test_own_api_detected_in_every_form():
    for url in ("http://127.0.0.1:8790/api/x", "http://localhost:8790/", "http://2130706433:8790/",
                "http://[::1]:5173/", "http://0.0.0.0:8790/"):
        assert resolve.is_own_api(resolve.host_of_url(url))
    assert resolve.is_own_api(HostUse("rebind.test", 8790, ("127.0.0.1",)))
    assert not resolve.is_own_api(resolve.host_of_url("http://127.0.0.1:3000/"))

@pytest.mark.parametrize("ip,local", [
    ("127.0.0.1", True), ("0.0.0.0", True), ("::1", True), ("10.1.2.3", True), ("172.16.0.1", True), ("172.32.0.1", False),
    ("192.168.1.1", True), ("169.254.169.254", True), ("100.64.0.1", True), ("fd00::1", True), ("fe80::1", True),
    ("93.184.216.34", False), ("2606:4700::1", False)])
def test_is_local(ip, local):
    assert resolve.is_local(ip) is local

async def test_resolve_ips_orders_addresses_and_only_uses_the_table(dns):
    table, lookups = dns
    table["x.test"] = [["2001:db8::1", "203.0.113.7", "203.0.113.7", "203.0.113.8"]]
    assert await resolve.resolve_ips("x.test") == ("203.0.113.7", "2001:db8::1", "203.0.113.8")
    assert await resolve.resolve_ips("10.0.0.5") == ("10.0.0.5",) and await resolve.resolve_ips("a.localhost") == ("127.0.0.1",)
    with pytest.raises(ToolError, match="cannot resolve nope.test"): await resolve.resolve_ips("nope.test")
    assert lookups == ["x.test", "nope.test"]

async def test_a_real_lookup_fails_the_test():                # no dns fixture: the autouse guard is active
    with pytest.raises(AssertionError, match="real DNS lookup"): await resolve.resolve_ips("example.com")

def test_paths_resolve_through_symlinks_and_dotdot(tmp_path, monkeypatch):
    env = tmp_path / "secret.env"; env.write_text("K=v"); monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    work = tmp_path / "work"; work.mkdir(); (work / "link").symlink_to(env)
    assert resolve.resolve_path("link", work) == env
    assert resolve.protected(resolve.resolve_path("link", work), "read")
    assert resolve.resolve_path("a/../../etc/passwd", work) == Path(os.path.realpath(tmp_path / "etc/passwd"))
    assert resolve.protected(work / "notes.txt", "write") is None
    assert not resolve.is_inside(resolve.resolve_path("../x", work), work)
    for p in (config.mcp_file(), config.chunker_path().parent / "x.py", config.PROJECT_ROOT / "anbu-system-one" / "index.js",
              config.db_path(), Path(str(config.db_path()) + "-wal"), Path(str(config.db_path()) + "-shm")):
        assert resolve.protected(p, "write") and resolve.protected(p, "read") is None
    environ = resolve.resolve_path("/proc/self/environ", work)
    assert resolve.protected(environ, "read") and resolve.protected(environ, "write")
    assert resolve.protected(Path("/proc/1/mem"), "read") and resolve.protected(Path("/proc/1/status"), "read") is None
    for raw in ("secret.env ", "notes.txt\n", str(env) + "\t"):                       # the chunker strips these
        with pytest.raises(ToolError, match="whitespace"): resolve.resolve_path(raw, tmp_path)

def test_sensitive_paths():
    home = Path.home()
    for p in (home / ".ssh" / "config", Path("/srv/app/.env.local"), Path("/tmp/server.pem"), Path("/srv/vault.kdbx"),
              config.mcp_file(), home / ".git-credentials", home / ".config" / "gh" / "hosts.yml", home / ".bash_history",
              home / ".mozilla" / "firefox" / "x" / "logins.json", home / ".local" / "share" / "keyrings" / "login.keyring"):
        assert "usually holds credentials" in resolve.sensitive(p) and resolve.sensitive(p, "write")
    assert resolve.sensitive(Path("/srv/app/main.py")) is None and resolve.sensitive(Path("/srv/app/main.py"), "write") is None
    for p in (Path("/srv/app/.git/hooks/pre-commit"), Path("/srv/app/.git/config"), home / ".bashrc",
              home / ".config" / "autostart" / "x.desktop", home / ".local" / "bin" / "nmap",
              config.PROJECT_ROOT / "backend" / "hunterai" / "permissions.py", config.PROJECT_ROOT / "scripts" / "start.sh",
              config.output_dir() / "r1" / "s1.txt", config.proc_dir() / "p1.log"):
        assert "can run code later or is part of HunterAI" in resolve.sensitive(p, "write") and resolve.sensitive(p, "read") is None
    assert resolve.sensitive(config.work_dir("c1") / "src" / "a.py", "write") is None

@pytest.mark.parametrize("entry,host,port,path,ok", [
    ("example.com", "example.com", 443, None, True), ("example.com", "a.example.com", 443, None, False),
    ("*.example.com", "a.b.example.com", 443, None, True), ("*.example.com", "example.com", 443, None, False),
    ("*.example.com", "notexample.com", 443, None, False), ("example.com:8443", "example.com", 443, None, False),
    ("10.0.0.0/24", "10.0.0.7", 80, None, True), ("https://example.com/app", "example.com", 443, "/app/x", True),
    ("example.com/app", "example.com", 443, "/other", False), ("???", "example.com", 443, None, False),
    ("example.com/app", "example.com", 443, "/app", True), ("example.com/app/", "example.com", 443, "/app/x", True),
    ("example.com/app", "example.com", 443, "/application", False),
    ("example.com/app", "example.com", 443, resolve.url_path("https://example.com/app/../admin"), False),
    ("example.com/admin", "example.com", 443, resolve.url_path("https://example.com/app/../admin"), True),
])
def test_scope_entry_matching(entry, host, port, path, ok):
    assert resolve.matches(entry, HostUse(host, port), path) is ok

def test_scope_verdict_rules():
    t = Target(id="t", name="n", notes="", scope={"in": ["*.acme.test"], "out": ["admin.acme.test"]}, created_at="x")
    v = resolve.scope_verdict(t, [HostUse("admin.acme.test", 443), HostUse("other.test", 443), HostUse("127.0.0.1", 3000)])
    assert v.out == ("admin.acme.test",) and v.not_in == ("other.test",)
    assert resolve.scope_verdict(None, [HostUse("x.test", 80)]) == resolve.ScopeVerdict((), ())
    assert resolve.scope_verdict(t, [HostUse("cdn.test", 443, ("10.9.9.9",))]).not_in == ("cdn.test",)
    net = Target(id="t", name="n", notes="", scope={"in": ["10.0.0.0/24"], "out": []}, created_at="x")
    two = ("203.0.113.9", "10.0.0.7")                              # the request goes to the first address
    assert resolve.scope_verdict(net, [HostUse("x.test", 443, two)]).not_in == ("x.test",)
    assert resolve.scope_verdict(net, [HostUse("y.test", 443, two[::-1])]).not_in == ()
    assert resolve.scope_verdict(net, [HostUse("z.test", 443)]).not_in == ("z.test",)          # not resolved yet
    bad = Target(id="t", name="n", notes="", scope={"in": [], "out": ["10.0.0.0/24"]}, created_at="x")
    assert resolve.scope_verdict(bad, [HostUse("x.test", 443, two)]).out == ("x.test",)       # any address is enough

def test_extract_hosts_is_best_effort():
    hosts = {h.host for h in resolve.extract_hosts("nmap -sV 10.0.0.5 && curl https://a.acme.test:8443/x -o out.txt; cat notes.md")}
    assert hosts == {"10.0.0.5", "a.acme.test"}
    local = resolve.extract_hosts("curl localhost:8790/api/chats -X PUT; curl 'http://[::1]:5173/'")
    assert HostUse("localhost", 8790) in local and all(resolve.is_own_api(h) for h in local)
