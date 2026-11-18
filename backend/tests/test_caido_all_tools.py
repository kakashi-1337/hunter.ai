"""While recording in Caido is on, what the tools send to a target goes through Caido: a download too, and a
scanner from the tool folder is not run until its command sends it through. What is read back from the history
leaves media out, shows text whatever it was sent as, and says what a certificate or another binary body is."""
import asyncio
import hashlib
import shutil

import httpx
import pytest
import respx

from hunterai import caido, toolbox, upstream
from hunterai.providers.base import ToolCall
from hunterai.repo import settings
from hunterai.tools import caido_tools, registry, shell, web
from tests.test_api import client  # noqa: F401 - a fixture: the API of a HunterAI with its own home
from tests.test_tools_caido import ROW, URL, FakeCaido
from tests.test_tools_web import IP, NET
from tests.test_tools_web import download as _download
from tests.toolkit import run_call, seeded_run

ALLOW = {"risk": {"execute": "allow"}}
ZIP = b"PK\x03\x04" + b"\x00" * 26 + b"backup/users.sql" + b"\x00" * 40 + b"backup/.env.production" + b"\x00" * 8


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    for t in (*web.tools(), *shell.tools(), *caido_tools.tools()):
        registry.register(t)
    monkeypatch.delenv("CAIDO_PROXY", raising=False)
    monkeypatch.setitem(caido._unreached, "until", 0.0)      # noqa: SLF001


async def download(conn, args, policy=None):
    return await asyncio.wait_for(_download(conn, args, policy=policy), 30)


async def called(conn, chat, run_id, tool: str, args: dict):
    """One tool call. A call that waits for the owner's answer fails here instead of waiting for ever."""
    return await asyncio.wait_for(run_call(conn, chat, run_id, ToolCall("c", tool, args)), 30)


def through_caido(monkeypatch, note: str = "") -> list:
    """Recording is on and Caido answers (or, with a note, does not). Returns the ways the web tools went out by."""
    made = []
    real = web._client     # noqa: SLF001
    monkeypatch.setattr(web, "_client", lambda via=None: (made.append(via), real(via))[1])

    async def via(ctx):
        return (None, note) if note else (("127.0.0.1", 18791), "")

    async def on(conn=None):
        return True
    monkeypatch.setattr(web, "_via_caido", via)
    monkeypatch.setattr(caido, "recording", on)
    return made


def served(body: bytes, ctype: str, **headers) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": ctype, **headers})


def saved(out) -> bytes:
    path = out.block.content.split("saved ", 1)[1].split(" (", 1)[0]
    with open(path, "rb") as f:
        return f.read()


# ---- downloads

@respx.mock
async def test_a_download_goes_through_caido_and_its_result_says_so(conn, dns, monkeypatch):
    dns[0]["files.test"] = [IP]
    made = through_caido(monkeypatch)
    named = respx.get("http://files.test/backup.zip").mock(return_value=served(ZIP, "application/zip"))
    pinned = respx.get(f"http://{IP}/backup.zip").mock(return_value=served(b"direct", "application/zip"))
    out, _ = await download(conn, {"url": "http://files.test/backup.zip"}, policy=NET)
    assert not out.block.is_error and out.block.content.endswith("content-type application/zip); recorded in Caido"), out.block.content
    assert saved(out) == ZIP and named.call_count == 1 and not pinned.called and made == [("127.0.0.1", 18791)]


@respx.mock
async def test_a_media_file_is_downloaded_without_caido_and_the_result_says_why(conn, dns, monkeypatch):
    dns[0]["files.test"] = [IP]
    made = through_caido(monkeypatch)
    named = respx.get("http://files.test/intro.MP4?v=2").mock(return_value=served(b"through", "video/mp4"))
    pinned = respx.get(f"http://{IP}/intro.MP4?v=2").mock(return_value=served(b"\x00\x00\x00 ftypisom", "video/mp4"))
    out, _ = await download(conn, {"url": "http://files.test/intro.MP4?v=2"}, policy=NET)
    assert out.block.content.endswith("; not recorded in Caido: a media file (.mp4)"), out.block.content
    assert saved(out) == b"\x00\x00\x00 ftypisom" and pinned.call_count == 1 and not named.called and made == [None]


@respx.mock
async def test_with_recording_off_a_download_is_what_it_was(conn, dns, monkeypatch):
    dns[0]["files.test"] = [IP]
    respx.get(f"http://{IP}/intro.mp4").mock(return_value=served(b"film", "video/mp4"))
    respx.get(f"http://{IP}/a.bin").mock(return_value=served(b"data", "application/octet-stream"))
    for name in ("intro.mp4", "a.bin"):
        out, _ = await download(conn, {"url": f"http://files.test/{name}"}, policy=NET)
        assert out.block.content.startswith("saved ") and "Caido" not in out.block.content


@respx.mock
async def test_an_answer_that_turns_out_to_be_media_is_fetched_again_without_caido(conn, dns, monkeypatch):
    """Its address did not say so. The first request stays in the history: that is the record of it."""
    dns[0]["files.test"] = [IP]
    made = through_caido(monkeypatch)
    named = respx.get("http://files.test/get?id=7").mock(return_value=served(b"through", "Video/MP4; codecs=avc1"))
    pinned = respx.get(f"http://{IP}/get?id=7").mock(return_value=served(b"the film", "video/mp4"))
    out, _ = await download(conn, {"url": "http://files.test/get?id=7"}, policy=NET)
    assert out.block.content.endswith("; not recorded in Caido: the answer is media (video/mp4), so the file was fetched again without "
                                      "Caido; the first request is in the history"), out.block.content
    assert saved(out) == b"the film" and named.call_count == 1 and pinned.call_count == 1 and made == [("127.0.0.1", 18791), None]


@respx.mock
async def test_a_file_too_large_to_record_is_fetched_again_without_caido(conn, dns, monkeypatch):
    dns[0]["files.test"] = [IP]
    made = through_caido(monkeypatch)
    monkeypatch.setattr(web, "RECORDED_DOWNLOAD_BYTES", 50)
    pulled = []

    async def in_parts():
        for n in range(51):
            pulled.append(n)
            yield b"x"
    named = respx.get("http://files.test/dump.sql").mock(return_value=httpx.Response(200, content=in_parts(), headers={
        "content-type": "application/sql", "content-length": "51"}))
    pinned = respx.get(f"http://{IP}/dump.sql").mock(return_value=served(b"y" * 51, "application/sql"))
    out, _ = await download(conn, {"url": "http://files.test/dump.sql"}, policy=NET)
    assert "; not recorded in Caido: larger than the " in out.block.content and "fetched again without Caido" in out.block.content
    assert saved(out) == b"y" * 51 and (named.call_count, pinned.call_count) == (1, 1) and made == [("127.0.0.1", 18791), None]
    assert pulled == []                 # it said how long it is: none of it was pulled through Caido to find out
    # exactly the limit is recorded
    respx.get("http://files.test/small.sql").mock(return_value=served(b"x" * 50, "application/sql"))
    out, _ = await download(conn, {"url": "http://files.test/small.sql"}, policy=NET)
    assert out.block.content.endswith("; recorded in Caido") and saved(out) == b"x" * 50


@respx.mock
async def test_a_file_of_unknown_length_that_grows_too_large_starts_again_without_caido(conn, dns, monkeypatch):
    dns[0]["files.test"] = [IP]
    through_caido(monkeypatch)
    monkeypatch.setattr(web, "RECORDED_DOWNLOAD_BYTES", 50)
    async def in_parts():                        # no Content-Length: the answer does not say how long it is
        yield b"a" * 40
        yield b"b" * 40
    respx.get("http://files.test/export").mock(return_value=httpx.Response(200, content=in_parts(),
                                                                           headers={"content-type": "application/json"}))
    whole = b"c" * 30 + b"d" * 60
    respx.get(f"http://{IP}/export").mock(return_value=served(whole, "application/json"))
    out, _ = await download(conn, {"url": "http://files.test/export"}, policy=NET)
    assert saved(out) == whole and f"({len(whole)} bytes, sha256 {hashlib.sha256(whole).hexdigest()}," in out.block.content
    assert "; not recorded in Caido: larger than the " in out.block.content


@respx.mock
async def test_a_caido_that_cannot_be_reached_does_not_stop_a_download(conn, dns, monkeypatch):
    dns[0]["files.test"] = [IP]
    made = through_caido(monkeypatch, note="not recorded in Caido: Caido does not answer at http://127.0.0.1:8791")
    respx.get(f"http://{IP}/a.json").mock(return_value=served(b"{}", "application/json"))
    out, _ = await download(conn, {"url": "http://files.test/a.json"}, policy=NET)
    assert out.block.content.endswith("; not recorded in Caido: Caido does not answer at http://127.0.0.1:8791") and made == [None]


@respx.mock
async def test_a_failed_download_through_caido_is_not_tried_again(conn, dns, monkeypatch):
    dns[0]["files.test"] = [IP]
    through_caido(monkeypatch)
    named = respx.get("http://files.test/gone.zip").mock(return_value=httpx.Response(404, text="no"))
    pinned = respx.get(f"http://{IP}/gone.zip").mock(return_value=served(b"direct", "application/zip"))
    out, _ = await download(conn, {"url": "http://files.test/gone.zip"}, policy=NET)
    assert out.block.is_error and "download failed: HTTP 404" in out.block.content and named.call_count == 1 and not pinned.called


async def test_a_chat_with_a_proxy_of_its_own_downloads_through_that_proxy_as_before(conn, dns, monkeypatch):
    """Recording does not take a download away from the proxy the program asked for, and Caido is not started for it."""
    dns[0]["files.acme.test"] = [IP]
    asked = []

    async def via(ctx):
        asked.append(1)
        return ("127.0.0.1", 18791), ""
    monkeypatch.setattr(web, "_via_caido", via)
    chat, run_id = await seeded_run(conn, scope={"in": ["*.acme.test"], "out": []}, permissions=NET)
    await upstream.put_target(conn, chat.target_id, "http://127.0.0.1:9")
    await settings.put(conn, "browser_via_caido", True)
    out, _, _ = await called(conn, chat, run_id, "web_download", {"url": "http://files.acme.test/a.zip"})
    assert asked == [] and "Caido" not in out.block.content and "the proxy at 127.0.0.1:9" in out.block.content     # nothing answers there


@respx.mock
async def test_a_local_address_the_owner_allowed_is_let_through_caidos_way_out_for_that_download(conn, monkeypatch):
    """Caido's way out refuses this machine and the local network; a call the owner allowed is the exception, while it runs."""
    made = through_caido(monkeypatch)
    during = []

    def answer(request):
        during.append(set(caido._state["egress"].local_ok.own))       # noqa: SLF001
        return served(b"{}", "application/json")
    respx.get("http://127.0.0.1:3000/export.json").mock(side_effect=answer)
    try:
        out, _ = await download(conn, {"url": "http://127.0.0.1:3000/export.json"}, policy={**NET, "domains": ["127.0.0.1"]})
        assert out.block.content.endswith("; recorded in Caido"), out.block.content
        assert during == [{("127.0.0.1", 3000)}] and made == [("127.0.0.1", 18791)]
        assert (await caido.egress()).local_ok.own == set()             # and not after it
    finally:
        await caido.shutdown()


def test_media_is_told_by_the_end_of_the_path_and_by_the_type():
    for path in ("/logo.png", "/a/b/Photo.JPEG?w=200", "/fonts/inter.woff2#x", "/v/clip.mp4", "/s.mp3", "https://cdn.test/i.webp"):
        assert caido.media_extension(path), path
    for path in ("/", "/api/users", "/app.js", "/app.js.map", "/data.json", "/icon.svg", "/style.css", "/main.ts", "/live.m3u8",
                 "/ca.pem", "/backup.zip", "/report.pdf", "/x.png/users", "/png", "/a.b/c", "/.png", "/dir/.mp4"):
        assert caido.media_extension(path) == "", path
    assert caido.media_extension("/A.PNG") == ".png"
    for kind in ("image/png", "IMAGE/JPEG", "audio/mpeg", "video/mp4; codecs=avc1", "font/woff2", "application/font-woff",
                 "application/vnd.ms-fontobject", "application/x-font-ttf"):
        assert caido.media_type(kind), kind
    for kind in ("", "text/html", "application/json", "image/svg+xml", "application/zip", "application/octet-stream",
                 "application/pdf", "application/x-x509-ca-cert"):
        assert not caido.media_type(kind), kind


# ---- a scanner from the tool folder is not run until it is sent through Caido

def a_program(name: str) -> None:
    path = toolbox.folder() / name
    path.write_text(f'#!/bin/sh\necho "{name} ran: $*"\n')
    path.chmod(0o755)


@pytest.fixture
def recording(monkeypatch):
    """Recording is on, Caido answers at 18791, and the tool folder holds the scanners and two other programs."""
    for name in ("httpx", "nuclei", "katana", "ffuf", "sqlmap", "subfinder", "naabu"):
        a_program(name)

    async def address():
        return "127.0.0.1", 18791
    monkeypatch.setattr(caido, "available", lambda: True)
    monkeypatch.setattr(caido, "proxy_address", address)


async def run(conn, command: str, on: bool = True, proxy: str | None = None):
    chat, run_id = await seeded_run(conn, scope={"in": ["*.acme.test"], "out": []}, permissions=ALLOW)
    if on:
        await settings.put(conn, "browser_via_caido", True)
    if proxy:
        await upstream.put_target(conn, chat.target_id, proxy)
    out, _, steps = await called(conn, chat, run_id, "execute", {"command": command, "timeout": 15})
    return out.block, steps


async def test_a_scanner_that_is_not_sent_through_caido_is_not_run(conn, recording):
    block, steps = await run(conn, "httpx -u https://shop.acme.test -silent -status-code")
    assert block.is_error and "ran:" not in block.content
    assert block.content.endswith(
        'Recording in Caido is switched on, and this command runs httpx without sending it through Caido, so nothing it sends would '
        'be recorded. Add -http-proxy "$CAIDO_PROXY" to httpx and send the command again. Only for a target on this machine or the '
        "local network, which Caido does not carry for a command: put CAIDO_PROXY= in front of httpx instead, and it runs "
        "unrecorded. Nothing was run."), block.content
    assert all(s.kind != "permission" for s in steps)            # held before the owner is asked anything


async def test_the_same_scanner_sent_through_caido_runs(conn, recording):
    block, _ = await run(conn, 'httpx -u https://shop.acme.test -silent -http-proxy "$CAIDO_PROXY"')
    assert not block.is_error and "httpx ran: -u https://shop.acme.test -silent -http-proxy http://127.0.0.1:18791" in block.content


@pytest.mark.parametrize("command,switch", [
    ("nuclei -u https://shop.acme.test -severity high", 'Add -proxy "$CAIDO_PROXY" to nuclei'),
    ("katana -u https://shop.acme.test -d 2", 'Add -proxy "$CAIDO_PROXY" to katana'),
    ("ffuf -u https://shop.acme.test/FUZZ -w words.txt", 'Add -replay-proxy "$CAIDO_PROXY" (only what matched; -x "$CAIDO_PROXY" '
                                                         "records every request) to ffuf"),
    ("sqlmap -u 'https://shop.acme.test/item?id=1' --batch", 'Add --proxy="$CAIDO_PROXY" to sqlmap'),
])
async def test_each_scanner_is_told_its_own_switch(conn, recording, command, switch):
    block, _ = await run(conn, command)
    assert block.is_error and switch in block.content and "ran:" not in block.content


async def test_the_hold_is_for_recording_without_a_proxy_of_the_owners(conn, recording, monkeypatch):
    command = "httpx -u https://shop.acme.test -silent"
    assert "httpx ran: -u https://shop.acme.test -silent" in (await run(conn, command, on=False))[0].content
    assert "httpx ran:" in (await run(conn, command, proxy="http://127.0.0.1:18080"))[0].content        # that proxy carries it
    monkeypatch.setattr(caido, "available", lambda: False)
    assert "httpx ran:" in (await run(conn, command))[0].content                                        # no Caido on this machine


async def test_a_scanner_that_is_not_in_the_tool_folder_is_none_of_hunterais(conn, recording):
    """Only what the owner put there under its usual name is known to be that program."""
    (toolbox.folder() / "httpx").unlink()
    block, _ = await run(conn, "httpx -u https://shop.acme.test -silent; echo went on")
    assert "Recording in Caido" not in block.content and "went on" in block.content


async def test_a_program_that_does_not_speak_http_to_the_target_is_not_held(conn, recording):
    for command, ran in (("subfinder -d shop.acme.test -silent", "subfinder ran:"), ("naabu -host shop.acme.test", "naabu ran:")):
        assert ran in (await run(conn, command))[0].content


def held(command: str, names=("httpx", "nuclei", "katana", "ffuf", "sqlmap")) -> str:
    found = shell.unrecorded(command, set(names))
    return found[0] if found else ""


def test_a_scanner_is_found_where_a_command_starts():
    for command in ("httpx -l hosts.txt", "  httpx -l hosts.txt", "cat hosts.txt | httpx -silent", "subfinder -d a.test -silent|httpx",
                    "cd out && httpx -l h.txt", "true; httpx -l h.txt", "echo start\nhttpx -l h.txt", "(httpx -l h.txt)",
                    "for h in a b; do httpx -u $h; done", "if true; then httpx -u a.test; fi", "nohup httpx -l h.txt &",
                    "time httpx -l h.txt", "./bin/httpx -l h.txt", "/home/me/hunterai/bin/httpx -l h", "RATE=5 httpx -l h.txt",
                    "out=$(httpx -u a.test -silent)", "echo `httpx -u a.test`", "sudo httpx -l h.txt"):
        assert held(command) == "httpx", command
    assert held("katana -u a.test | httpx -silent -http-proxy http://127.0.0.1:8080") == "katana"       # the first one found
    assert held("ffuf -u https://a.test/FUZZ -w w.txt") == "ffuf" and held("sqlmap -u 'https://a.test/?id=1'") == "sqlmap"


def test_a_name_that_is_not_run_is_left_alone():
    for command in ("pip install httpx", "which nuclei", "echo httpx", "cat httpx.txt", "ls bin/ | grep katana", "man ffuf",
                    "go install github.com/projectdiscovery/httpx/cmd/httpx@latest", "python3 -c 'import httpx'",
                    'grep -E "nuclei|httpx" notes.md', "ps aux | grep -E 'nuclei|httpx'", 'echo "then: httpx -l hosts.txt"',
                    "echo 'a; nuclei -u x'", "httpx-toolkit -l h.txt", "my-httpx -l h.txt", "nuclei_templates/run.sh",
                    "tail -f nuclei.log", "ls # httpx -l hosts", "ls  # before: cat hosts | httpx -silent; nuclei -l hosts",
                    "subfinder -d a.test -silent", "curl https://a.test"):
        assert held(command) == "", command
    assert held("httpx -l hosts.txt", names=("nuclei",)) == ""            # not a program of this tool folder


def test_asking_a_scanner_about_itself_is_not_scanning():
    for command in ("httpx -h", "httpx -version", "nuclei -version", "nuclei -update-templates", "nuclei -ut", "nuclei -up",
                    "katana --help", "ffuf -V", "ffuf -h", "sqlmap --version", "sqlmap -hh", "sqlmap --update", "nuclei -tl",
                    "httpx -h | head -40", "nuclei -help | grep -i proxy"):
        assert held(command) == "", command
    assert held("nuclei -ut; nuclei -u https://a.test") == "nuclei"       # the update is not what holds the scan back


def test_a_command_that_names_the_variable_is_not_held():
    for command in ('httpx -l h.txt -http-proxy "$CAIDO_PROXY"', "nuclei -l h.txt -proxy $CAIDO_PROXY", "ffuf -u a.test/FUZZ -w w -x ${CAIDO_PROXY}",
                    "CAIDO_PROXY= httpx -u http://127.0.0.1:3000", 'P="$CAIDO_PROXY"; katana -u a.test -proxy "$P"'):
        assert held(command) == "", command


async def test_the_model_is_told_that_a_scanner_is_held(conn):
    text = caido.commands_rule(True, ["execute"])
    assert "(httpx, nuclei, katana, ffuf, sqlmap) that is not sent through Caido is not run" in text


# ---- reading the history: media is left out of the list

def a_row(n: int, path: str) -> dict:
    return {**ROW, "id": str(n), "method": "GET", "path": path, "query": "", "response": {**ROW["response"], "id": str(n), "statusCode": 200}}


class History(FakeCaido):
    """A Caido whose history is `rows`, newest first."""

    def __init__(self, rows):
        super().__init__()
        self.rows, self.asked = rows, []

    def graphql(self, request):
        import json
        body = json.loads(request.content)
        v = body.get("variables") or {}
        if "nodes" in body["query"] and request.headers.get("authorization") == f"Bearer {self.valid}":
            self.asked.append((v.get("l"), v.get("o")))
            at = int(v.get("o") or 0)
            return httpx.Response(200, json={"data": {"requestsByOffset": {"count": {"value": len(self.rows)},
                                                                           "nodes": self.rows[at:at + int(v.get("l") or 0)]}}})
        return super().graphql(request)


@pytest.fixture
async def history(monkeypatch):
    for name in ("HUNTERAI_CAIDO", "HUNTERAI_CAIDO_URL", "HUNTERAI_CAIDO_PORT", "HUNTERAI_CAIDO_BIN"):
        monkeypatch.delenv(name, raising=False)
    for key in ("token", "egress", "routed"):
        monkeypatch.setitem(caido._state, key, None)     # noqa: SLF001
    monkeypatch.setattr(caido, "binary", lambda: "/usr/bin/caido-cli")

    def of(rows):
        f = History(rows)
        respx.get(URL + "/").mock(side_effect=f.root)
        respx.post(URL + "/graphql").mock(side_effect=f.graphql)
        return f
    with respx.mock:
        yield of
        await caido.shutdown()


PAGE = ["/", "/app.js", "/logo.png", "/api/me", "/fonts/inter.woff2", "/hero.JPG", "/api/cart", "/intro.mp4", "/style.css", "/icon.svg"]


async def listed(conn, **args) -> str:
    chat, run_id = await seeded_run(conn, mode="pentesting")
    out, _, _ = await called(conn, chat, run_id, "caido", {"action": "requests", **args})
    return out.block.content


async def test_the_list_leaves_media_out_and_says_how_many(conn, history):
    history([a_row(len(PAGE) - i, p) for i, p in enumerate(PAGE)])
    text = await listed(conn)
    assert text.splitlines()[0] == ("10 recorded request(s) match; rows 1-10, newest first (4 media requests left out: images, fonts, "
                                    "audio, video; media=true lists them):")
    assert [line.split(" ")[2].rsplit("app.acme.test", 1)[1] for line in text.splitlines()[1:]] == \
        ["/", "/app.js", "/api/me", "/api/cart", "/style.css", "/icon.svg"]


async def test_media_is_listed_when_it_is_asked_for(conn, history):
    history([a_row(len(PAGE) - i, p) for i, p in enumerate(PAGE)])
    text = await listed(conn, media=True)
    assert text.splitlines()[0] == "10 recorded request(s) match; rows 1-10, newest first:" and len(text.splitlines()) == 11
    assert "/logo.png" in text and "/intro.mp4" in text


async def test_a_list_without_media_reads_as_it_did(conn, history):
    history([a_row(3 - i, p) for i, p in enumerate(["/api/a", "/api/b", "/api/c"])])
    assert (await listed(conn)).splitlines()[0] == "3 recorded request(s) match; rows 1-3, newest first:"


async def test_the_list_is_filled_from_further_rows_and_says_where_to_go_on(conn, history):
    """A page of a site is mostly pictures: the rows asked for are found behind them."""
    paths = ["/img/1.png", "/api/1", "/img/2.png", "/img/3.png", "/api/2", "/img/4.png", "/api/3", "/api/4", "/img/5.png", "/api/5"]
    f = history([a_row(len(paths) - i, p) for i, p in enumerate(paths)])
    text = await listed(conn, limit=3)
    lines = text.splitlines()
    assert lines[0] == ("10 recorded request(s) match; rows 1-7, newest first (4 media requests left out: images, fonts, audio, video; "
                        "media=true lists them):")
    assert [line.split(" ")[2][-6:] for line in lines[1:4]] == ["/api/1", "/api/2", "/api/3"] and lines[4] == "more: offset=7"
    assert f.asked[0] == (3, 0)                                 # the first look is the one it always was
    text = await listed(conn, limit=3, offset=7)
    assert text.splitlines()[0].startswith("10 recorded request(s) match; rows 8-10, newest first (1 media request left out:")
    assert "more:" not in text and "/api/4" in text and "/api/5" in text


async def test_a_history_of_media_only_says_so(conn, history):
    history([a_row(2, "/a.png"), a_row(1, "/b.gif")])
    assert await listed(conn) == "2 recorded request(s) match, all of them media (images, fonts, audio, video): media=true lists them."
    history([])
    assert await listed(conn) == "No recorded request matches."


# ---- reading one exchange: what a body is, whatever it was sent as

def body(kind: str, data: bytes, **more) -> str:
    head = "HTTP/1.1 200 OK\r\n" + (f"Content-Type: {kind}\r\n" if kind else "") + "".join(f"{k}: {v}\r\n" for k, v in more.items())
    return caido_tools.render(head.encode() + b"\r\n" + data).split("\n\n", 1)[1]


def test_media_is_one_line_as_before():
    assert body("image/png", b"\x89PNG") == "[binary body, 4 bytes, image/png]"
    assert body("font/woff2", b"wOF2" + b"\x00" * 50) == "[binary body, 54 bytes, font/woff2]"
    assert body("video/mp4", b"\x00\x00\x00 ftypisom some words here") == "[binary body, 28 bytes, video/mp4]"


def test_text_is_shown_whatever_it_was_sent_as():
    assert body("application/octet-stream", b'{"role":"admin","id":7}') == '[sent as application/octet-stream; it is text]\n{"role":"admin","id":7}'
    assert body("application/x-pem-file", b"-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n").startswith(
        "[sent as application/x-pem-file; it is text]\n-----BEGIN PRIVATE KEY-----")
    assert body("application/zip", "ünïcode text, not a zip".encode()).endswith("ünïcode text, not a zip")
    assert body("text/plain", b"plain") == "plain" and body("application/json", b"{}") == "{}"      # what said it is text reads as before
    assert "it is text" not in body("application/octet-stream", b"abc\x00def") and "it is text" not in body("application/octet-stream", b"\x1b[31mred")
    long = body("application/octet-stream", b"x" * 30_000)
    assert "[body cut at 20000 characters of 30000]" in long


def test_another_binary_body_says_what_it_looks_like_and_shows_the_words_in_it():
    text = body("application/zip", ZIP)
    assert text.splitlines()[0] == f"[binary body, {len(ZIP)} bytes, application/zip: it looks like a zip archive (also jar, apk, docx)]"
    assert text.splitlines()[1] == "words in it: backup/users.sql | backup/.env.production"
    assert body("application/octet-stream", b"\xac\xed\x00\x05sr\x00\x11java.util.HashMap\x05\x07").splitlines()[0].endswith(
        "it looks like a serialized Java object]")
    assert body("application/octet-stream", b"\x01\x02\x03\x04\x05") == "[binary body, 5 bytes, application/octet-stream]"


def test_the_words_of_a_binary_body_are_kept_short():
    data = b"\x00".join(b"word%04d-" % i + b"x" * 200 for i in range(200))
    text = body("application/octet-stream", data)
    words = text.splitlines()[1]
    assert words.startswith("words in it: word0000-") and len(words) <= caido_tools.MAX_WORDS_CHARS + 40 and words.endswith("[more left out]")
    assert "x" * 121 not in words                                # each word is cut
    far = b"\x00" * (caido_tools.WORDS_FROM_BYTES + 10) + b"hidden-word-after-the-part-that-is-read"
    assert "hidden-word" not in body("application/octet-stream", far)


def a_certificate() -> tuple[bytes, bytes]:
    """(DER, PEM) of a certificate made for this test."""
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Acme"), x509.NameAttribute(NameOID.COMMON_NAME, "internal.acme.test")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(4660)
            .not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)).not_valid_after(datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("internal.acme.test"), x509.DNSName("*.corp.acme.test")]), critical=False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER), cert.public_bytes(serialization.Encoding.PEM)


def test_a_certificate_is_read_out():
    pytest.importorskip("cryptography")
    der, pem = a_certificate()
    text = body("application/pkix-cert", der)
    lines = text.splitlines()
    assert lines[0] == f"[certificate, {len(der)} bytes, application/pkix-cert]"
    assert "subject: CN=internal.acme.test,O=Acme" in lines and "issuer: CN=internal.acme.test,O=Acme (the same: it signed itself)" in lines
    assert "valid: 2026-01-01 to 2027-01-01" in lines and "names: internal.acme.test, *.corp.acme.test" in lines and "serial: 1234" in lines
    assert text.rstrip().endswith("-----END CERTIFICATE-----") and "-----BEGIN CERTIFICATE-----" in text
    # sent as plain bytes, it is still a certificate; sent as PEM, it is text and reads as it is
    assert body("application/octet-stream", der).splitlines()[0] == f"[certificate, {len(der)} bytes, application/octet-stream]"
    assert body("application/x-x509-ca-cert", pem).startswith("[sent as application/x-x509-ca-cert; it is text]\n-----BEGIN CERTIFICATE-----")


def test_a_certificate_is_still_shown_where_it_cannot_be_read_out(monkeypatch):
    pytest.importorskip("cryptography")
    der, _ = a_certificate()
    monkeypatch.setattr(caido_tools, "_read_certificate", lambda data: None)          # the library is not there, or the bytes are not one
    text = body("application/pkix-cert", der)
    assert text.splitlines()[0] == f"[certificate, {len(der)} bytes, application/pkix-cert: not read out here]"
    assert "-----BEGIN CERTIFICATE-----" in text and "subject:" not in text
    assert body("application/octet-stream", der).startswith(f"[binary body, {len(der)} bytes, application/octet-stream")   # only bytes, then


# ---- the panel reads the same list

async def test_the_panels_list_leaves_media_out_too(client, monkeypatch):
    from hunterai.api import system

    async def up():
        return True
    rows = [a_row(3, "/api/me"), a_row(2, "/logo.png"), a_row(1, "/app.js")]

    async def past(filter_code, limit, offset):
        return rows[offset:offset + limit], len(rows)
    monkeypatch.setattr(caido, "available", lambda: True)
    monkeypatch.setattr(system, "_caido_up", up)
    monkeypatch.setattr(caido, "history", past)
    got = (await client.get("/api/caido/requests")).json()
    assert (got["total"], got["left_out"], [r["url"][-7:] for r in got["rows"]]) == (3, 1, ["/api/me", "/app.js"])
    got = (await client.get("/api/caido/requests", params={"media": "true"})).json()
    assert (got["left_out"], len(got["rows"])) == (0, 3)


pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="no shell")
