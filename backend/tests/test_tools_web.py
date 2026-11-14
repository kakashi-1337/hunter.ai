import asyncio
import hashlib
import types

import httpx
import pytest
import respx

from hunterai.providers.base import ToolCall
from hunterai.repo import chats, runs
from hunterai.tools import registry, runner, web
from tests.toolkit import answer_next, run_call, seeded_run

NET = {"risk": {"network": "allow"}}
IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def web_registered():
    for t in web.tools():
        registry.register(t)


async def _run(conn, tool, args, policy, mode, scope):
    chat, run_id = await seeded_run(conn, mode=mode, scope=scope, permissions=policy or {})
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c1", tool, args))
    return out, chat


async def fetch(conn, args, policy=None, mode="pentesting", scope=None):
    return (await _run(conn, "web_fetch", args, policy, mode, scope))[0]


async def download(conn, args, policy=None, mode="pentesting"):
    return await _run(conn, "web_download", args, policy, mode, None)


async def asked(conn, args, decision, policy=None, scope=None, tool="web_fetch"):
    task = asyncio.create_task(_run(conn, tool, args, policy, "pentesting", scope))
    req = await answer_next(None, decision)
    return req, (await task)[0]


def text(body, ctype="text/plain", status=200, **headers):
    return httpx.Response(status, text=body, headers={"content-type": ctype, **headers})


@respx.mock
async def test_request_goes_to_the_address_that_was_checked(conn, dns):                # Review Focus 4
    table, lookups = dns; table["example.test"] = ["93.184.216.34", "127.0.0.1"]       # a second lookup would rebind to loopback
    good = respx.get("http://93.184.216.34/a").mock(return_value=httpx.Response(200, text="hello", headers={"content-type": "text/plain"}))
    local = respx.get("http://127.0.0.1/a").mock(return_value=httpx.Response(200, text="secret"))
    out = await fetch(conn, {"url": "http://example.test/a"}, policy=NET)
    assert "HTTP 200 http://example.test/a" in out.block.content and "hello" in out.block.content
    assert good.calls.last.request.headers["host"] == "example.test" and lookups == ["example.test"] and not local.called

@respx.mock
async def test_cross_host_redirect_is_returned_not_followed(conn, dns):                # Review Focus 4
    table, _ = dns; table["a.test"] = ["93.184.216.34"]; table["evil.test"] = ["203.0.113.9"]
    respx.get("http://93.184.216.34/go").mock(return_value=httpx.Response(302, headers={"location": "http://evil.test/x"}))
    evil = respx.get("http://203.0.113.9/x").mock(return_value=httpx.Response(200, text="pwned"))
    out = await fetch(conn, {"url": "http://a.test/go"}, policy=NET)
    assert "redirect (302) to http://evil.test/x was not followed: it leaves a.test" in out.block.content and not evil.called

@pytest.mark.parametrize("url,shown", [("http://good.test@evil.test/", "evil.test"), ("http://bücher.example/", "xn--bcher-kva.example"),
                                       ("http://0x7f.0.0.1:9000/", "127.0.0.1"), ("http://[::ffff:10.0.0.5]/", "10.0.0.5")])
async def test_the_ask_shows_the_real_host(conn, dns, url, shown):                     # Review Focus 4
    dns[0].update({"evil.test": ["203.0.113.9"], "xn--bcher-kva.example": ["203.0.113.8"]})
    task = asyncio.create_task(fetch(conn, {"url": url}))
    req = await answer_next(None, "deny")
    assert req.hosts[0]["host"] == shown and (await task).block.content.startswith("The owner denied this call.")
    assert dns[1] == []                                     # nothing was looked up for a call the owner denied

async def test_denied_fetch_sends_no_dns_query(conn, dns):                             # Ruling 19: a host name can carry data out
    task = asyncio.create_task(fetch(conn, {"url": "http://c2VjcmV0.evil.test/"}))
    req = await answer_next(None, "deny"); out = await task
    assert req.hosts == [{"host": "c2vjcmv0.evil.test", "port": 80, "ips": []}] and runner.NOT_RESOLVED in req.reasons
    assert dns[1] == [] and out.block.content.startswith("The owner denied this call.")

@respx.mock
async def test_dotted_and_encoded_paths_are_checked_as_sent(conn, dns):                # Review Focus 4
    dns[0]["a.test"] = ["93.184.216.34"]
    scope = {"in": ["a.test/app"], "out": ["a.test/admin"]}
    task = asyncio.create_task(fetch(conn, {"url": "https://a.test/app/../admin"}, policy=NET, scope=scope))
    req = await answer_next(None, "deny"); await task
    assert req.can_allow_chat is False and any("out-of-scope list" in w for w in req.warnings)     # checked as /admin
    out = await fetch(conn, {"url": "https://a.test/app/%2e%2e/admin"}, policy=NET, scope=scope)
    assert out.block.is_error and "encoded dots or slashes" in out.block.content
    task = asyncio.create_task(fetch(conn, {"url": "https://a.test/application"}, policy=NET, scope=scope))
    req = await answer_next(None, "deny"); await task
    assert any("not in this target's in-scope list" in w for w in req.warnings) and not respx.calls

@pytest.mark.parametrize("url", ["http://127.0.0.1:8790/api/chats", "http://localhost:5173/", "http://2130706433:8790/", "http://[::1]:8790/"])
async def test_own_api_is_denied_even_when_network_is_allowed(conn, dns, url):
    assert "permission policy (own-api)" in (await fetch(conn, {"url": url}, policy=NET)).block.content

@respx.mock
async def test_name_resolving_to_loopback_asks_with_a_warning(conn, dns):
    dns[0]["rebind.test"] = ["127.0.0.1"]
    local = respx.get("http://127.0.0.1/").mock(return_value=text("secret"))
    warning = "rebind.test resolves to 127.0.0.1, an address on this machine or local network"
    req, _ = await asked(conn, {"url": "http://rebind.test/"}, "deny", policy=NET)
    assert warning in req.warnings and req.hosts[0]["ips"] == ["127.0.0.1"]     # resolved first: one ask
    task = asyncio.create_task(fetch(conn, {"url": "http://rebind.test/"}))      # default preset
    first = await answer_next(None, "allow_once")
    second = await answer_next(None, "deny")
    await task
    assert first.hosts[0]["ips"] == [] and warning in second.warnings and not local.called


@respx.mock
async def test_local_services_ask_even_when_network_is_allowed(conn, dns):
    tags = respx.get("http://127.0.0.1:11434/api/tags").mock(return_value=text("{}", "application/json"))
    for url in ("http://127.0.0.1:11434/api/tags", "http://192.168.1.1/"):
        req, _ = await asked(conn, {"url": url}, "deny", policy=NET)
        assert any("is an address on this machine or local network" in w for w in req.warnings)
    assert not tags.called
    out = await fetch(conn, {"url": "http://127.0.0.1:11434/api/tags"},
                      policy={**NET, "domains": ["127.0.0.1:11434"]})
    assert "HTTP 200" in out.block.content and tags.called


@respx.mock
async def test_out_of_scope_host_asks_and_out_list_cannot_be_allowed_for_chat(conn, dns):
    dns[0].update({"other.test": ["203.0.113.5"], "admin.acme.test": [IP], "a.acme.test": [IP]})
    scope = {"in": ["*.acme.test"], "out": ["admin.acme.test"]}
    ok = respx.get(f"http://{IP}/").mock(return_value=text("fine"))
    req, _ = await asked(conn, {"url": "http://other.test/"}, "deny", policy=NET, scope=scope)
    assert any("other.test is not in this target's in-scope list" in w for w in req.warnings)
    req, _ = await asked(conn, {"url": "http://admin.acme.test/"}, "deny", policy=NET, scope=scope)
    assert req.can_allow_chat is False and not ok.called
    assert "fine" in (await fetch(conn, {"url": "http://a.acme.test/"}, policy=NET, scope=scope)).block.content


@respx.mock
async def test_same_host_redirects_are_followed_up_to_five(conn, dns):
    dns[0]["a.test"] = [IP]
    for i in range(3):
        respx.get(f"http://{IP}/h{i}").mock(return_value=httpx.Response(302, headers={"location": f"/h{i + 1}"}))
    respx.get(f"http://{IP}/h3").mock(return_value=text("arrived"))
    out = await fetch(conn, {"url": "http://a.test/h0"}, policy=NET)
    assert "HTTP 200 http://a.test/h3" in out.block.content and "arrived" in out.block.content
    assert "redirects: 302 -> http://a.test/h1" in out.block.content
    respx.get(f"http://{IP}/loop").mock(return_value=httpx.Response(302, headers={"location": "/loop"}))
    out = await fetch(conn, {"url": "http://a.test/loop"}, policy=NET)
    assert out.block.is_error and "5 redirects" in out.block.content
    respx.get(f"http://{IP}/up").mock(return_value=httpx.Response(301, headers={"location": "https://a.test/up"}))
    secure = respx.get(f"https://{IP}/up").mock(return_value=text("tls"))
    out = await fetch(conn, {"url": "http://a.test/up"}, policy=NET)
    assert "tls" in out.block.content and secure.calls.last.request.extensions["sni_hostname"] == "a.test"


@respx.mock
async def test_redirect_out_of_a_path_scope_is_not_followed(conn, dns):
    dns[0]["a.test"] = [IP]
    respx.get(f"http://{IP}/app/x").mock(return_value=httpx.Response(302, headers={"location": "/admin"}))
    admin = respx.get(f"http://{IP}/admin").mock(return_value=text("admin"))
    out = await fetch(conn, {"url": "http://a.test/app/x"}, policy=NET, scope={"in": ["a.test/app"], "out": []})
    assert "was not followed: it leaves this target's scope" in out.block.content and not admin.called


@respx.mock
async def test_https_sends_sni_and_host(conn, dns):
    dns[0]["example.test"] = [IP]
    route = respx.get(f"https://{IP}/").mock(return_value=text("ok"))
    await fetch(conn, {"url": "https://example.test/"}, policy=NET)
    request = route.calls.last.request
    assert request.extensions["sni_hostname"] == "example.test" and request.headers["host"] == "example.test"
    assert request.headers["user-agent"] == web.USER_AGENT
    route = respx.get(f"http://{IP}:8080/").mock(return_value=text("ok"))
    await fetch(conn, {"url": "http://example.test:8080/"}, policy=NET)
    assert route.calls.last.request.headers["host"] == "example.test:8080"


@respx.mock
async def test_post_headers_and_forbidden_headers(conn, dns):
    dns[0]["a.test"] = [IP]
    route = respx.post(f"http://{IP}/api").mock(return_value=text("{}", "application/json"))
    out = await fetch(conn, {"url": "http://a.test/api", "method": "POST", "body": '{"a":1}',
                             "headers": {"X-Test": "1", "Content-Type": "application/json"}}, policy=NET)
    request = route.calls.last.request
    assert request.content == b'{"a":1}' and request.headers["x-test"] == "1" and "HTTP 200" in out.block.content
    for bad in ({"headers": {"Host": "evil.test"}}, {"body": "x"},
                {"method": "POST", "body": "x" * (web.MAX_BODY_BYTES + 1)}):
        out = await fetch(conn, {"url": "http://a.test/api", **bad}, policy=NET)
        assert out.block.is_error and not out.invalid
    assert route.call_count == 1


@respx.mock
async def test_html_is_reduced_and_raw_is_not(conn, dns):
    dns[0]["a.test"] = [IP]
    page = ("<html><head><title>My Page</title><style>p{}</style></head><body><script>evil()</script>"
            "<h1>Hi &amp; bye</h1><p>See <a href='https://x.test/d'>Docs</a></p></body></html>")
    respx.get(f"http://{IP}/p").mock(return_value=text(page, "text/html; charset=utf-8", **{"x-a": "b"}))
    out = (await fetch(conn, {"url": "http://a.test/p"}, policy=NET)).block.content
    body = out.split("\n\n", 1)[1]
    assert body.startswith("My Page") and "Hi & bye" in body and "Docs [https://x.test/d]" in body
    assert "evil()" not in out and "<h1>" not in out
    raw = (await fetch(conn, {"url": "http://a.test/p", "raw": True}, policy=NET)).block.content
    assert "<h1>Hi &amp; bye</h1>" in raw and "x-a: b" in raw
    assert web.html_to_text("<p>a</p><p>b</p>") == "a\n\nb"


@respx.mock
async def test_body_cap_and_binary_note(conn, dns):
    dns[0]["a.test"] = [IP]
    respx.get(f"http://{IP}/big").mock(return_value=text("x" * (3 * 1024**2)))
    respx.get(f"http://{IP}/img").mock(return_value=httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"}))
    out = (await fetch(conn, {"url": "http://a.test/big"}, policy=NET)).block.content
    assert "characters omitted" in out                       # the runner's cap names the saved full output
    out = (await fetch(conn, {"url": "http://a.test/img"}, policy=NET)).block.content
    assert "[binary body, 4 bytes, image/png; use web_download]" in out


@respx.mock
async def test_fetch_body_is_cut_at_two_mib(conn, dns, monkeypatch):
    monkeypatch.setattr(web, "MAX_FETCH_BYTES", 1000)
    dns[0]["a.test"] = [IP]
    respx.get(f"http://{IP}/big").mock(return_value=text("Q" * 5000))
    out = (await fetch(conn, {"url": "http://a.test/big"}, policy=NET)).block.content
    assert out.count("Q") == 1000 and "[body cut at" in out


@respx.mock
async def test_unresolvable_host_and_bad_scheme(conn, dns):
    out = await fetch(conn, {"url": "http://nope.test/"}, policy=NET)
    assert out.block.is_error and "cannot resolve nope.test" in out.block.content
    out = await fetch(conn, {"url": "file:///etc/passwd"}, policy=NET)
    assert out.block.is_error and "http" in out.block.content and not respx.calls


@respx.mock
async def test_slow_server_hits_the_deadline(conn, dns, monkeypatch):
    monkeypatch.setattr(web, "FETCH_DEADLINE", 0.3)
    dns[0]["a.test"] = [IP]

    async def drip():
        yield b"x"
        await asyncio.sleep(30)
    respx.get(f"http://{IP}/slow").mock(return_value=httpx.Response(200, content=drip(), headers={"content-type": "text/plain"}))
    out = await fetch(conn, {"url": "http://a.test/slow"}, policy=NET)
    assert out.block.is_error and "web_fetch stopped: it took longer than" in out.block.content


@respx.mock
async def test_download_lands_in_the_work_folder_with_a_safe_name(conn, dns):
    dns[0]["files.test"] = [IP]
    respx.get(f"http://{IP}/x").mock(return_value=httpx.Response(200, content=b"payload", headers={"content-type": "application/x-sh"}))
    chat, run_id = await seeded_run(conn, permissions=NET)
    folder = chats.resolve_workdir(chat) / "downloads"
    for n, name in enumerate(("evil.sh", "evil-1.sh")):
        out, _, _ = await run_call(conn, chat, run_id, ToolCall(f"c{n}", "web_download", {
            "url": "http://files.test/x", "filename": "../../evil.sh"}))
        assert out.block.content == (f"saved {folder / name} (7 bytes, sha256 "
                                     f"{hashlib.sha256(b'payload').hexdigest()}, content-type application/x-sh)")
    assert sorted(p.name for p in folder.iterdir()) == ["evil-1.sh", "evil.sh"]
    assert (folder / "evil.sh").read_bytes() == b"payload"


@respx.mock
async def test_download_respects_size_cap_and_disk_floor(conn, dns, monkeypatch):
    dns[0]["files.test"] = ["93.184.216.34"]
    route = respx.get("http://93.184.216.34/big.bin").mock(
        return_value=httpx.Response(200, content=b"x" * (2 * 1024**2), headers={"content-type": "application/octet-stream"}))
    out, chat = await download(conn, {"url": "http://files.test/big.bin", "max_mb": 1}, policy=NET)
    assert out.block.is_error and "larger than the limit of 1 MB" in out.block.content
    assert list((chats.resolve_workdir(chat) / "downloads").glob("*")) == []                  # no file and no .part
    monkeypatch.setattr(web.shutil, "disk_usage", lambda path: types.SimpleNamespace(total=0, used=0, free=5 * 1024**3))
    before = route.call_count
    out, chat = await download(conn, {"url": "http://files.test/big.bin"}, policy=NET)
    assert out.block.is_error and "not enough free disk: 5.0 GiB free, the floor is 10 GiB" in out.block.content
    assert route.call_count == before and list((chats.resolve_workdir(chat) / "downloads").glob("*")) == []

@respx.mock
async def test_download_cancel_removes_the_part_file(conn, dns):
    dns[0]["files.test"] = ["93.184.216.34"]
    reading = asyncio.Event()
    async def body():
        yield b"x" * 65_536
        reading.set()
        await asyncio.sleep(30)
    respx.get("http://93.184.216.34/slow.bin").mock(return_value=httpx.Response(200, content=body()))
    task = asyncio.create_task(download(conn, {"url": "http://files.test/slow.bin"}, policy=NET))
    await asyncio.wait_for(reading.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    chat = (await chats.list(conn))[0]
    assert list((chats.resolve_workdir(chat) / "downloads").glob("*")) == []                  # the .part file is gone
    run = await runs.get(conn, (await runs.list_for_chat(conn, chat.id))[0].id)
    assert (run.steps[-1].kind, run.steps[-1].status) == ("tool_call", "cancelled")

async def test_download_cannot_be_steered_out_of_the_work_folder(conn, dns, tmp_path):        # Review Focus 4
    chat, run_id = await seeded_run(conn, permissions=NET)
    work = chats.resolve_workdir(chat); work.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"; outside.mkdir(); (work / "downloads").symlink_to(outside)
    out, _, steps = await run_call(conn, chat, run_id, ToolCall("c1", "web_download", {"url": "http://files.test/a.bin"}))
    assert out.block.is_error and "outside the working folder" in out.block.content and list(outside.glob("*")) == []
    assert dns[1] == [] and all(s.kind != "permission" for s in steps)                         # stopped in prepare
    plan, plan_run = await seeded_run(conn, mode="plan", permissions=NET)
    out, _, _ = await run_call(conn, plan, plan_run, ToolCall("c2", "web_download", {"url": "http://files.test/a.bin"}))
    assert out.invalid and 'Unknown tool "web_download"' in out.block.content                  # plan mode does not offer it
    out, _, _ = await run_call(conn, plan, plan_run, ToolCall("c3", "web_download", {"url": "http://files.test/a.bin"}), offered={"web_download"})
    assert "permission policy (mode)" in out.block.content and dns[1] == []                    # switched to plan during a run


@respx.mock
async def test_with_the_caido_switch_on_a_fetch_goes_through_caido_by_name(conn, dns, monkeypatch):
    table, _ = dns; table["example.test"] = [IP]
    made = []
    real = web._client
    monkeypatch.setattr(web, "_client", lambda via=None: (made.append(via), real(via))[1])

    async def via(ctx):
        return ("127.0.0.1", 18791), ""
    monkeypatch.setattr(web, "_via_caido", via)
    named = respx.get("http://example.test/a").mock(return_value=text("hello"))
    pinned = respx.get(f"http://{IP}/a").mock(return_value=text("direct"))
    out = await fetch(conn, {"url": "http://example.test/a"}, policy=NET)
    assert "hello" in out.block.content and "recorded in Caido" in out.block.content
    assert named.called and not pinned.called and made == [("127.0.0.1", 18791)]
    assert named.calls.last.request.headers["user-agent"] == web.USER_AGENT

    # Caido is switched on but does not answer: the fetch is made directly, and the result says it was not recorded.
    async def broken(ctx):
        return None, "not recorded in Caido: Caido does not answer"
    monkeypatch.setattr(web, "_via_caido", broken)
    out = await fetch(conn, {"url": "http://example.test/a"}, policy=NET)
    assert "direct" in out.block.content and "not recorded in Caido: Caido does not answer" in out.block.content


async def test_the_caido_switch_decides_whether_a_fetch_is_recorded(conn, monkeypatch):
    from hunterai import caido
    from hunterai.repo import settings

    async def address():
        return "127.0.0.1", 18791
    monkeypatch.setattr(caido, "proxy_address", address)
    monkeypatch.setattr(caido, "available", lambda: True)
    assert await web._via_caido(None) == (None, "")                      # the switch is off
    await settings.put(conn, "browser_via_caido", True)
    assert await web._via_caido(None) == (("127.0.0.1", 18791), "")

    async def down():
        raise caido.CaidoError("Caido does not answer at http://127.0.0.1:8791")
    monkeypatch.setattr(caido, "proxy_address", down)
    assert await web._via_caido(None) == (None, "not recorded in Caido: Caido does not answer at http://127.0.0.1:8791")
    monkeypatch.setattr(caido, "available", lambda: False)                # no Caido on this machine: nothing to say
    assert await web._via_caido(None) == (None, "")
