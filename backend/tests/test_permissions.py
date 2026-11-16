import dataclasses
from pathlib import Path

import pytest
from pydantic import ValidationError

from hunterai import permissions
from hunterai.permissions import Grant, Policy
from hunterai.repo import chats, grants
from hunterai.tools.base import HostUse, PathUse, Prepared, Tool, ToolResult
from hunterai.types import Target

NET = Policy(risk={"network": "allow"})


def T(risk, name="t", source="native"):
    async def h(ctx, args):
        return ToolResult("")
    return Tool(name, "d", {"type": "object"}, risk, h, source=source)


def d(**kw):
    args = dict(mode="pentesting", tool=T("read"), prepared=Prepared(), target=None,
                workdir=Path("/nonexistent-workdir"), global_policy=Policy(),
                chat_policy=Policy(), grants=[])
    args.update(kw)
    return permissions.decide(**args)


@pytest.mark.parametrize("mode,risk,want", [
    ("plan", "read", "allow"), ("plan", "network", "ask"), ("plan", "write", "deny"), ("plan", "execute", "deny"),
    ("pentesting", "read", "allow"), ("pentesting", "write", "ask"), ("pentesting", "execute", "ask"), ("pentesting", "network", "ask"),
    ("coding", "read", "allow"), ("coding", "write", "ask"), ("coding", "execute", "ask"), ("coding", "network", "ask"),
    ("bogus", "execute", "ask"),
])
def test_mode_presets(mode, risk, want): assert d(mode=mode, tool=T(risk)).decision == want

def test_override_order_most_specific_wins():
    g, c = Policy(risk={"execute": "deny"}, tools={"t": "ask"}), Policy(risk={"execute": "allow"})
    assert d(tool=T("execute"), global_policy=g).rule == "global-tool"
    assert d(tool=T("execute"), global_policy=g, chat_policy=c).decision == "allow"
    assert d(tool=T("execute"), chat_policy=Policy(risk={"execute": "allow"}, tools={"t": "deny"})).decision == "deny"

def test_plan_hard_deny_beats_every_override_and_grant():
    v = d(mode="plan", tool=T("execute"), chat_policy=Policy(tools={"t": "allow"}, risk={"execute": "allow"}),
          prepared=Prepared(grant_target="id"), grants=[Grant("t", "id", True)])
    assert (v.decision, v.rule) == ("deny", "mode")
    assert permissions.offered("plan", T("execute"), Policy(), Policy(tools={"t": "allow"})) is False
    assert permissions.offered("pentesting", T("execute"), Policy(), Policy()) is True
    assert permissions.offered("pentesting", dataclasses.replace(T("read"), expose=False), Policy(), Policy()) is False
    dl = Prepared(paths=(PathUse("/w/downloads/d.bin", "write"),), hosts=(HostUse("x.test", 443),), grant_target="g")
    v = d(mode="plan", tool=T("network"), prepared=dl, chat_policy=NET, grants=[Grant("t", "g", True)])     # a download writes a file
    assert (v.decision, v.rule) == ("deny", "mode")
    assert d(mode="plan", tool=T("network"), prepared=Prepared(hosts=(HostUse("x.test", 443),))).decision == "ask"
    downloader = dataclasses.replace(T("network"), writes_files=True)
    assert permissions.offered("plan", downloader, Policy(), Policy()) is False and permissions.offered("coding", downloader, Policy(), Policy()) is True

def test_coding_allows_writes_inside_workdir_only(tmp_path):
    inside = Prepared(paths=(PathUse(str(tmp_path / "a.py"), "write"),))
    outside = Prepared(paths=(PathUse("/etc/hosts", "write"),))
    assert d(mode="coding", tool=T("write"), prepared=inside, workdir=tmp_path).decision == "allow"
    assert d(mode="coding", tool=T("write"), prepared=outside, workdir=tmp_path).decision == "ask"
    assert d(mode="pentesting", tool=T("write"), prepared=inside, workdir=tmp_path).decision == "ask"
    assert d(tool=T("write"), prepared=outside, chat_policy=Policy(write_paths=["/etc"])).decision == "allow"
    dl = Prepared(paths=(PathUse(str(tmp_path / "d.bin"), "write"),), hosts=(HostUse("x.test", 443),), grant_target="g")
    assert d(mode="coding", tool=T("network"), prepared=dl, workdir=tmp_path).decision == "ask"   # write-path rule is for write tools only

def test_write_inside_the_workdir_still_asks_for_paths_that_run_code(tmp_path):
    hook = Prepared(paths=(PathUse(str(tmp_path / ".git" / "hooks" / "pre-commit"), "write"),), grant_target="h")
    src = Prepared(paths=(PathUse(str(tmp_path / "src" / "a.py"), "write"),), grant_target="s")
    v = d(mode="coding", tool=T("write"), prepared=hook, workdir=tmp_path)
    assert v.decision == "ask" and "can run code later" in v.warnings[0]
    assert d(mode="coding", tool=T("write"), prepared=src, workdir=tmp_path).decision == "allow"
    assert d(mode="coding", tool=T("read"), prepared=Prepared(paths=(PathUse(hook.paths[0].path, "read"),))).decision == "allow"

def test_read_allowlist_upgrades_ask_only():
    ASK = Policy(risk={"read": "ask"})
    docs = Prepared(paths=(PathUse("/srv/docs/a.txt", "read"),))
    other = Prepared(paths=(PathUse("/srv/other/a.txt", "read"),))
    assert d(tool=T("read"), prepared=docs, chat_policy=ASK).decision == "ask"
    assert d(tool=T("read"), prepared=docs, chat_policy=ASK, global_policy=Policy(read_paths=["/srv/docs"])).decision == "allow"
    assert d(tool=T("read"), prepared=docs, chat_policy=Policy(risk={"read": "ask"}, read_paths=["/srv/docs"])).decision == "allow"
    assert d(tool=T("read"), prepared=other, chat_policy=ASK, global_policy=Policy(read_paths=["/srv/docs"])).decision == "ask"
    assert d(tool=T("read"), prepared=docs, chat_policy=Policy(tools={"t": "deny"}),
             global_policy=Policy(read_paths=["/srv/docs"])).decision == "deny"

def test_protected_path_is_denied_whatever_the_policy(tmp_path, monkeypatch):
    env = tmp_path / ".env"; env.write_text("x"); monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env))
    v = d(tool=T("read"), prepared=Prepared(paths=(PathUse(str(env), "read"),)), chat_policy=Policy(tools={"t": "allow"}))
    assert (v.decision, v.rule) == ("deny", "protected")

def test_sensitive_path_asks_until_granted_with_the_warning_seen():
    p = Prepared(paths=(PathUse(str(Path.home() / ".ssh" / "id_ed25519"), "read"),), grant_target="k")
    v = d(tool=T("read"), prepared=p)
    assert v.decision == "ask" and "usually holds credentials" in v.warnings[0] and v.can_allow_chat
    assert d(tool=T("read"), prepared=p, grants=[Grant("t", "k", False)]).decision == "ask"
    assert d(tool=T("read"), prepared=p, grants=[Grant("t", "k", True)]).decision == "allow"

def test_domain_allowlist_and_grants_upgrade_ask_only():
    p = Prepared(hosts=(HostUse("docs.test", 443),), grant_target="GET https://docs.test:443")
    assert d(tool=T("network"), prepared=p).decision == "ask"
    assert d(tool=T("network"), prepared=p, global_policy=Policy(domains=["docs.test"])).decision == "allow"
    assert d(tool=T("network"), prepared=p, grants=[Grant("t", p.grant_target, False)]).decision == "allow"
    assert d(tool=T("network"), prepared=p, grants=[Grant("other", p.grant_target, False)]).decision == "ask"
    assert d(tool=T("network"), prepared=p, grants=[Grant("t", "GET https://other.test:443", False)]).decision == "ask"
    assert d(tool=T("network"), prepared=dataclasses.replace(p, grant_target=""), grants=[Grant("t", "", False)]).decision == "ask"
    assert d(tool=T("network"), prepared=p, chat_policy=Policy(tools={"t": "deny"}), grants=[Grant("t", p.grant_target, True)]).decision == "deny"

def test_scope_escalates_allow_to_ask_and_deny_stays_deny():
    t = Target(id="t", name="n", notes="", scope={"in": ["*.acme.test"], "out": ["admin.acme.test"]}, created_at="x")
    off = Prepared(hosts=(HostUse("other.test", 443),), grant_target="o")
    out = Prepared(hosts=(HostUse("admin.acme.test", 443),), grant_target="a")
    v = d(tool=T("network"), prepared=off, target=t, chat_policy=NET)
    assert v.decision == "ask" and "not in this target's in-scope list" in v.warnings[0] and v.can_allow_chat
    v = d(tool=T("network"), prepared=out, target=t, chat_policy=NET, grants=[Grant("t", "a", True)])
    assert v.decision == "ask" and "out-of-scope list" in v.warnings[0] and v.can_allow_chat is False
    assert d(tool=T("network"), prepared=off, target=t, chat_policy=NET, grants=[Grant("t", "o", False)]).decision == "ask"
    assert d(tool=T("network"), prepared=off, target=t, chat_policy=NET, grants=[Grant("t", "o", True)]).decision == "allow"
    assert d(tool=T("network"), prepared=off, target=t, chat_policy=Policy(risk={"network": "allow"}, domains=["other.test"])).decision == "allow"
    ok = Prepared(hosts=(HostUse("a.acme.test", 443),), grant_target="i")
    assert d(tool=T("execute"), prepared=ok, target=t).warnings == ()
    assert d(mode="plan", tool=T("execute"), prepared=off, target=t).decision == "deny"
    v = d(tool=T("read"), prepared=off, target=t)                      # host rules follow the hosts, not the risk class
    assert v.decision == "ask" and "not in this target's in-scope list" in v.warnings[0]

def test_an_out_of_scope_line_that_cannot_be_read_makes_every_host_ask():
    t = Target(id="t", name="n", notes="", scope={"in": ["*.acme.test"], "out": ["*.old.*", "admin.acme.test"]}, created_at="x")
    ok = Prepared(hosts=(HostUse("shop.acme.test", 443),), grant_target="s")
    v = d(tool=T("network"), prepared=ok, target=t, chat_policy=NET)
    assert v.decision == "ask" and v.can_allow_chat
    assert v.warnings == ("this target's out-of-scope line cannot be read, so nothing is checked against it: *.old.*",)
    assert d(tool=T("network"), prepared=ok, target=t, chat_policy=NET, grants=[Grant("t", "s", True)]).decision == "allow"
    assert d(tool=T("network"), prepared=ok, target=t, chat_policy=NET, grants=[Grant("t", "s", False)]).decision == "ask"
    assert d(tool=T("read"), prepared=Prepared(), target=t).warnings == ()                       # a call that names no host
    fixed = Target(id="t", name="n", notes="", scope={"in": ["*.acme.test", "*.new.*"], "out": ["admin.acme.test"]}, created_at="x")
    assert d(tool=T("network"), prepared=ok, target=fixed, chat_policy=NET).decision == "allow"   # an in-scope line that cannot be read only fails to include

def test_own_api_and_local_addresses():
    own = Prepared(hosts=(HostUse("127.0.0.1", 8790, ("127.0.0.1",)),), grant_target="x")
    assert d(tool=T("network"), prepared=own, chat_policy=NET).rule == "own-api"
    assert d(tool=T("read"), prepared=own).rule == "own-api" and d(tool=T("write"), prepared=own).rule == "own-api"
    assert d(tool=T("execute", source="mcp:x"), prepared=own, chat_policy=Policy(risk={"execute": "allow"})).rule == "own-api"
    v = d(tool=T("execute"), prepared=own, chat_policy=Policy(risk={"execute": "allow"}))     # the native shell: ask, never for the chat
    assert v.decision == "ask" and v.can_allow_chat is False and "HunterAI's own API" in " ".join(v.warnings)
    named = Prepared(hosts=(HostUse("localhost", 8790),), grant_target="x")                   # what extract_hosts gives for "curl localhost:8790"
    assert d(tool=T("execute"), prepared=named, chat_policy=Policy(risk={"execute": "allow"})).can_allow_chat is False
    rebind = Prepared(hosts=(HostUse("evil.test", 80, ("127.0.0.1",)),), grant_target="r")
    v = d(tool=T("network"), prepared=rebind, chat_policy=NET)
    assert v.decision == "ask" and "evil.test resolves to 127.0.0.1, an address on this machine or local network" in v.warnings
    for host, port in (("127.0.0.1", 11434), ("192.168.1.1", 80), ("10.0.0.7", 6333)):
        local = Prepared(hosts=(HostUse(host, port, (host,)),), grant_target="l")
        v = d(tool=T("network"), prepared=local, chat_policy=NET)
        assert v.decision == "ask" and f"{host} is an address on this machine or local network" in v.warnings and v.can_allow_chat
        assert d(tool=T("network"), prepared=local, chat_policy=NET, grants=[Grant("t", "l", True)]).decision == "allow"
        assert d(tool=T("network"), prepared=local, chat_policy=NET, grants=[Grant("t", "l", False)]).decision == "ask"
    dev = Prepared(hosts=(HostUse("127.0.0.1", 3000, ("127.0.0.1",)),), grant_target="d")
    assert d(tool=T("network"), prepared=dev, chat_policy=Policy(risk={"network": "allow"}, domains=["127.0.0.1:3000"])).decision == "allow"
    lab = Target(id="t", name="n", notes="", scope={"in": ["10.0.0.0/24", "*.corp.test"], "out": []}, created_at="x")
    inside = Prepared(hosts=(HostUse("10.0.0.7", 80, ("10.0.0.7",)),), grant_target="i")
    assert d(tool=T("network"), prepared=inside, target=lab, chat_policy=NET).decision == "allow"       # the owner listed the range
    by_name = Prepared(hosts=(HostUse("app.corp.test", 80, ("192.168.5.5",)),), grant_target="n")
    assert d(tool=T("network"), prepared=by_name, target=lab, chat_policy=NET).decision == "ask"        # an in-scope name is not enough
    assert d(tool=T("network"), prepared=Prepared(hosts=(HostUse("pub.test", 443, ("93.184.216.34",)),)), chat_policy=NET).decision == "allow"

def test_may_resolve_only_for_names_the_owner_already_trusts():
    t = Target(id="t", name="n", notes="", scope={"in": ["*.acme.test"], "out": []}, created_at="x")
    kw = dict(tool=T("network"), global_policy=Policy(), chat_policy=Policy(), grants=[])
    p = lambda host: Prepared(hosts=(HostUse(host, 443),), grant_target=f"GET https://{host}:443", resolve_hosts=True)
    assert permissions.may_resolve(prepared=p("a.acme.test"), target=t, **kw) is True
    assert permissions.may_resolve(prepared=p("evil.test"), target=t, **kw) is False
    assert permissions.may_resolve(prepared=p("evil.test"), target=None, **kw) is False            # no in-scope list: nothing is trusted
    assert permissions.may_resolve(prepared=p("docs.test"), target=None, **{**kw, "chat_policy": Policy(domains=["docs.test"])}) is True
    assert permissions.may_resolve(prepared=p("evil.test"), target=t, **{**kw, "grants": [Grant("t", "GET https://evil.test:443", False)]}) is True

def test_policy_rejects_relative_paths_and_unknown_fields():
    with pytest.raises(ValidationError): Policy(write_paths=["relative/dir"])
    with pytest.raises(ValidationError): Policy(bypass=True)

async def test_grants_repo(conn):
    a = await chats.create(conn, None, "coding", "mock", "m", {})
    b = await chats.create(conn, None, "coding", "mock", "m", {})
    await grants.add(conn, a.id, "a", "x", False)
    await grants.add(conn, a.id, "a", "x", True)          # warned only ever goes false -> true
    await grants.add(conn, a.id, "a", "y", True)
    await grants.add(conn, a.id, "a", "y", False)
    await grants.add(conn, a.id, "b", "z", False)
    await grants.add(conn, b.id, "a", "x", False)
    got = sorted(await grants.list(conn, a.id), key=lambda g: (g.tool, g.target))
    assert got == [Grant("a", "x", True), Grant("a", "y", True), Grant("b", "z", False)]
    assert await grants.clear_tools(conn, a.id, ["a"]) == 2
    assert await grants.list(conn, a.id) == [Grant("b", "z", False)]
    assert await grants.clear_tools(conn, a.id, []) == 0
    assert await grants.clear(conn, a.id) == 1 and await grants.list(conn, a.id) == []
    await chats.delete(conn, b.id)
    assert await grants.list(conn, b.id) == []
