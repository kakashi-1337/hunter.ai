import asyncio
import pytest

from hunterai.kb import crawl, gitsrc, limits, sources
from hunterai.kb.limits import Budget
from hunterai.repo import kb
from hunterai.tools.base import ToolError
from tests.kb_samples import HANG, assert_dead, collect, make_collection
from tests.test_api import client  # noqa: F401

FAKE_GIT = r'''#!/bin/sh
echo "$@" > "$FAKE_OUT/git.args"
env > "$FAKE_OUT/git.env"
for a in "$@"; do dest="$a"; done
mkdir -p "$dest/.git" "$dest/src" "$dest/node_modules/x"
echo "ref: refs/heads/main" > "$dest/.git/HEAD"
printf '# Demo repo\n\nfinds idor bugs\n' > "$dest/README.md"
printf 'def scan():\n    return 1\n' > "$dest/src/scan.py"
echo "SECRET=1" > "$dest/.env"
echo "x" > "$dest/node_modules/x/i.js"
ln -s /etc/passwd "$dest/link"
'''


@pytest.mark.parametrize("url,label", [("https://github.com/owner/repo", "github.com/owner/repo"), ("https://github.com/owner/repo.git", "github.com/owner/repo"),
                                       ("https://gitlab.com/group/sub/repo/", "gitlab.com/group/sub/repo")])
def test_git_urls_accepted(url, label):
    assert gitsrc.validate(url) == (f"https://{label}.git", label)


@pytest.mark.parametrize("url", ["http://github.com/o/r", "https://evil.example/o/r", "https://user:pw@github.com/o/r", "git@github.com:o/r.git",
                                 "https://github.com/o", "https://github.com/o/r?x=1", "file:///etc", "https://github.com/o/../r", "ext::sh -c id", "--upload-pack=x", ""])
def test_git_urls_refused(url):
    with pytest.raises(ValueError):
        gitsrc.validate(url)


async def git_payload(url="https://github.com/owner/repo"):
    return {"collection_id": "c", "staging_id": sources.new_staging("c"), "source": {"kind": "git", "url": url}}


async def test_git_source_clones_safely_and_follows_the_folder_rules(fake_bin, tmp_path):
    out = fake_bin("git", FAKE_GIT.replace("$FAKE_OUT", str(tmp_path)))      # git gets a bare environment
    payload = await git_payload()
    found, problems = await collect(payload)
    assert sorted(f.uri for f in found) == ["git:github.com/owner/repo/README.md", "git:github.com/owner/repo/src/scan.py"]
    assert {(q.uri.removeprefix("git:github.com/owner/repo/"), q.reason) for q in problems} == {
        (".env", "secret-like file name (not ingested)"), ("node_modules/", "ignored directory"), ("link", "symlink not followed")}
    args = (out / "git.args").read_text().split()
    assert args[-2] == "https://github.com/owner/repo.git" and args[-3] == "--" and "--depth" in args and "--no-recurse-submodules" in args
    assert "protocol.file.allow=never" in args and "core.hooksPath=/dev/null" in args
    env = (out / "git.env").read_text()
    assert "GIT_TERMINAL_PROMPT=0" in env and "ABLIT" not in env and "ANTHROPIC" not in env
    assert not (sources.staging_path(payload["staging_id"]) / "repo" / ".git").exists()


async def test_git_failures_are_one_failed_record(fake_bin, monkeypatch, tmp_path):
    fake_bin("git", "#!/bin/sh\necho 'fatal: repository not found' >&2\nexit 128\n")
    found, problems = await collect(await git_payload())
    assert found == [] and [(q.status, q.reason) for q in problems] == [("failed", "git clone failed: fatal: repository not found")]
    found, problems = await collect(await git_payload("https://evil.example/x/y"))
    assert found == [] and problems[0].status == "failed" and "GitHub or GitLab" in problems[0].reason
    out = fake_bin("git", HANG.replace("$FAKE_OUT", str(tmp_path))); monkeypatch.setattr(gitsrc, "CLONE_TIMEOUT", 0.3)
    found, problems = await collect(await git_payload())
    assert [q.reason for q in problems] == ["git clone timed out after 0.3 s"]
    assert_dead(int((out / "main.pid").read_text()))


class FakeSite:
    """Stands in for crawl.Fetcher: url -> (status, headers, body)."""

    def __init__(self, pages):
        self.pages, self.asked = pages, []

    async def get(self, url):
        self.asked.append(url)
        if url not in self.pages:
            return 404, {}, b"", False
        status, ctype, body = self.pages[url]
        if isinstance(body, Exception):
            raise body
        headers = {"location": body.decode()} if 300 <= status < 400 else {"content-type": ctype}
        return status, headers, b"" if 300 <= status < 400 else body, False

    async def close(self):
        pass


def page(*hrefs, text="page"):
    return (200, "text/html; charset=utf-8", ("<html><body><p>%s</p>%s</body></html>" % (text, "".join(f'<a href="{h}">l</a>' for h in hrefs))).encode())


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    async def none(_):
        return None
    monkeypatch.setattr(crawl, "_sleep", none)


async def run_crawl(tmp_path, site, **source):
    found, problems = [], []
    async for x in crawl.crawl({"url": "https://acme.test/", **source}, tmp_path / "web", Budget(), site):
        (found if isinstance(x, crawl.Found) else problems).append(x)
    return found, problems


async def test_crawl_stays_on_the_site_within_depth_and_follows_robots(tmp_path):
    site = FakeSite({
        "https://acme.test/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /admin\n"),
        "https://acme.test/": page("/docs", "/admin", "https://other.test/x", "/file.pdf", "#top", "mailto:a@b", "/docs"),
        "https://acme.test/docs": page("/docs/deep", text="docs"),
        "https://acme.test/docs/deep": page("/docs/deeper"),
        "https://acme.test/file.pdf": (200, "application/pdf", b"%PDF-1.4 x"),
        "https://other.test/x": page(),
    })
    found, problems = await run_crawl(tmp_path, site, max_depth=1)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/docs", "https://acme.test/file.pdf"]
    assert [f.path.suffix for f in found] == [".html", ".html", ".pdf"] and all(f.path.is_file() for f in found)
    assert [(q.uri, q.reason) for q in problems] == [("https://acme.test/admin", "disallowed by robots.txt")]
    assert "https://other.test/x" not in site.asked and "https://acme.test/docs/deep" not in site.asked     # off-site, and too deep


async def test_crawl_limits_types_redirects_and_errors(tmp_path):
    site = FakeSite({
        "https://acme.test/": page("/a", "/b", "/img.png", "/moved", "/away", "/down", "/c"),
        "https://acme.test/a": page(), "https://acme.test/b": page(), "https://acme.test/c": page(),
        "https://acme.test/img.png": (200, "image/png", b"\x89PNG"),
        "https://acme.test/moved": (302, "", b"/a"),
        "https://acme.test/away": (301, "", b"https://evil.test/"),
        "https://acme.test/down": (200, "", ToolError("timed out")),
    })
    found, problems = await run_crawl(tmp_path, site, max_pages=3, robots=False)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/a", "https://acme.test/b"]
    assert "https://acme.test/robots.txt" not in site.asked
    assert (problems[-1].uri, problems[-1].reason) == ("https://acme.test/#crawl", "page limit reached (3 pages): 5 links not visited")
    found, problems = await run_crawl(tmp_path / "2", site, robots=False)
    assert {(q.uri, q.status, q.reason) for q in problems} == {
        ("https://acme.test/img.png", "skipped", "content type image/png is not ingested"),
        ("https://acme.test/away", "skipped", "redirect leaves the site (evil.test)"),
        ("https://acme.test/down", "failed", "timed out")}
    assert [f.uri for f in found].count("https://acme.test/a") == 1          # the redirect target is not saved twice


async def test_local_addresses_are_refused_unless_the_owner_allows_them(dns):
    table, _ = dns
    table["intranet.test"] = ["10.0.0.5"]; table["rebind.test"] = [["93.184.216.34", "127.0.0.1"]]
    for host in ("intranet.test", "rebind.test", "127.0.0.1", "localhost"):
        f = crawl.Fetcher()
        with pytest.raises(ToolError, match="this machine or local network"):
            await f.get(f"http://{host}/")
        await f.close()
    f = crawl.Fetcher(allow_local=True)
    assert await f._ip("intranet.test") == "10.0.0.5"
    await f.close()


@pytest.mark.parametrize("url", ["ftp://x.test/", "file:///etc/passwd", "https://user:pw@x.test/", "javascript:alert(1)", "", "x.test"])
def test_crawl_urls_refused(url):
    with pytest.raises(ValueError):
        crawl.validate(url)


async def test_web_source_through_discover_and_api(client, conn, tmp_path, monkeypatch):
    site = FakeSite({"https://acme.test/": page("/n", text="waf bypass notes"), "https://acme.test/n": page(text="second page")})
    monkeypatch.setattr(crawl, "Fetcher", lambda allow_local=False: site)
    cid = (await client.post("/api/kb/collections", json={"name": "Web", "embedder": "fake:64"})).json()["id"]
    assert (await client.post(f"/api/kb/collections/{cid}/ingest/web", json={"url": "ftp://x"})).status_code == 400
    assert (await client.post(f"/api/kb/collections/{cid}/ingest/git", json={"url": "https://evil.example/a/b"})).status_code == 400
    job = (await client.post(f"/api/kb/collections/{cid}/ingest/web", json={"url": "https://acme.test/", "robots": False})).json()
    assert job["payload"]["source"]["kind"] == "web" and job["payload"]["source"]["max_pages"] == 200
    from hunterai import worker
    assert await worker.drain(conn) == 1
    docs = (await client.get(f"/api/kb/collections/{cid}/documents")).json()["items"]
    assert {(d["source_uri"], d["status"], d["source_kind"]) for d in docs} == {("https://acme.test/", "ok", "web"), ("https://acme.test/n", "ok", "web")}
    hits = (await client.post("/api/kb/search", json={"collection_ids": [cid], "query": "waf bypass"})).json()["hits"]
    assert hits[0]["source_uri"] == "https://acme.test/" and "waf bypass notes" in hits[0]["text"]
    git = (await client.post(f"/api/kb/collections/{cid}/ingest/git", json={"url": "https://github.com/o/r"})).json()
    assert git["payload"]["source"] == {"kind": "git", "url": "https://github.com/o/r.git"}


# ---- review fixes

async def test_bad_links_do_not_end_a_crawl(tmp_path):
    site = FakeSite({"https://acme.test/": page("/a&#1;b", "http://acme.test:99999/x", "https://acme.test:PORT/y", "/" + "q" * 3000, "/ok"),
                     "https://acme.test/ok": page(text="fine")})
    found, problems = await run_crawl(tmp_path, site, robots=False)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/ok"]
    assert [(q.uri, q.reason) for q in problems] == [("https://acme.test/ab", "HTTP 404")]     # the control character is dropped; the rest never asked
    assert len(site.asked) == 3


async def test_requests_and_remembered_links_are_bounded(tmp_path, monkeypatch):
    pages = {"https://acme.test/": page(*[f"/gone{i}" for i in range(100)])}
    site = FakeSite(pages)
    found, problems = await run_crawl(tmp_path, site, max_pages=2, robots=False)
    assert len(site.asked) == 2 * crawl.REQUESTS_PER_PAGE + 10
    assert problems[-1].uri == "https://acme.test/#crawl" and "request limit reached (16 requests)" in problems[-1].reason
    monkeypatch.setattr(crawl, "MAX_QUEUE", 5)
    site = FakeSite({"https://acme.test/": page(*[f"/p{i}" for i in range(50)])})
    await run_crawl(tmp_path / "q", site, max_pages=100, robots=False)
    assert len([u for u in site.asked if "llms" not in u]) == 5    # the start page plus four remembered links


async def test_robots_rules_are_read_through_a_redirect(tmp_path):
    site = FakeSite({"https://acme.test/robots.txt": (301, "", b"https://acme.test/real-robots.txt"),
                     "https://acme.test/real-robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /private\n"),
                     "https://acme.test/": page("/private", "/public"), "https://acme.test/public": page()})
    found, problems = await run_crawl(tmp_path, site)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/public"]
    assert [(q.uri, q.reason) for q in problems] == [("https://acme.test/private", "disallowed by robots.txt")]


async def test_a_crawl_notices_cancel_at_once(conn, tmp_path, monkeypatch):
    from hunterai.kb import pipeline
    from hunterai.repo import jobs
    col = await make_collection(conn)
    site = FakeSite({"https://acme.test/": page(*[f"/p{i}" for i in range(30)]), **{f"https://acme.test/p{i}": page() for i in range(30)}})
    monkeypatch.setattr(crawl, "Fetcher", lambda allow_local=False: site)
    job = await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id),
                                               "source": {"kind": "web", "url": "https://acme.test/", "robots": False}})
    real = site.get

    async def get(url):
        if len(site.asked) == 4:
            await jobs.request(conn, job.id, "cancel")
        return await real(url)
    site.get = get
    assert await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"])) == "cancelled"
    assert len(site.asked) <= 7


# ---------------------------------------------------------------- pages that need JavaScript, and feeds

SHELL = (200, "text/html", b'<html><body><div id="root"></div><script src="/app.js"></script></body></html>')
PROSE = "The export endpoint trusts the id in the path. " * 8


class FakeRenderer:
    def __init__(self, pages):
        self.pages, self.asked, self.closed = pages, [], False

    async def page(self, url):
        self.asked.append(url)
        got = self.pages.get(url)
        if isinstance(got, Exception):
            raise got
        return got

    async def close(self):
        self.closed = True


async def rendered_crawl(tmp_path, site, renderer, **source):
    found, problems = [], []
    async for x in crawl.crawl({"url": "https://acme.test/", "render": True, **source}, tmp_path / "web", Budget(), site, renderer=renderer):
        (found if isinstance(x, crawl.Found) else problems).append(x)
    return found, problems


async def test_a_page_that_is_empty_without_javascript_is_rendered_and_its_links_followed(tmp_path):
    site = FakeSite({"https://acme.test/": SHELL, "https://acme.test/post": page(text=PROSE)})
    r = FakeRenderer({"https://acme.test/": f'<html><body><article>{PROSE}</article><a href="/post">next</a></body></html>'})
    found, problems = await rendered_crawl(tmp_path, site, r, robots=False)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/post"] and not problems
    assert "export endpoint" in found[0].path.read_text() and r.asked == ["https://acme.test/"]     # the page with text of its own is not rendered


async def test_rendering_is_off_unless_asked_for_and_a_failed_render_keeps_the_page(tmp_path):
    r = FakeRenderer({"https://acme.test/": "<html><body>rendered</body></html>"})
    found, _ = [], None
    async for x in crawl.crawl({"url": "https://acme.test/", "robots": False}, tmp_path / "a", Budget(), FakeSite({"https://acme.test/": SHELL}), renderer=r):
        found.append(x)
    assert r.asked == [] and len(found) == 1
    broken = FakeRenderer({"https://acme.test/": ToolError("the browser could not start")})
    found, problems = await rendered_crawl(tmp_path, FakeSite({"https://acme.test/": SHELL}), broken, robots=False)
    assert len(found) == 1 and b'id="root"' in found[0].path.read_bytes()
    assert [(q.status, q.reason) for q in problems] == [("skipped", "pages that need JavaScript were kept as fetched: the browser could not start")]


async def test_only_so_many_pages_are_rendered(tmp_path, monkeypatch):
    monkeypatch.setattr(crawl, "MAX_RENDERED", 2)
    shell = lambda *hrefs: (200, "text/html", ('<html><body><script>x</script>' + "".join(f'<a href="{h}">l</a>' for h in hrefs) + "</body></html>").encode())  # noqa: E731
    site = FakeSite({"https://acme.test/": shell("/a", "/b", "/c"), "https://acme.test/a": shell(), "https://acme.test/b": shell(), "https://acme.test/c": shell()})
    r = FakeRenderer({})
    found, problems = await rendered_crawl(tmp_path, site, r, robots=False)
    assert len(found) == 4 and len(r.asked) == 2 and any("render limit" in q.reason for q in problems)


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>
<item><title>One</title><link>https://acme.test/posts/one</link></item>
<item><title>Two</title><link>/posts/two</link></item>
<item><title>Elsewhere</title><link>https://other.test/x</link></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><link rel="alternate" href="https://acme.test/posts/one"/></entry>
<entry><link href="https://acme.test/posts/three"/><link rel="edit" href="https://acme.test/edit/3"/></entry></feed>"""


def test_feed_links_from_rss_and_atom():
    assert crawl.feed_links(RSS, "https://acme.test/feed.xml") == ["https://acme.test/posts/one", "https://acme.test/posts/two", "https://other.test/x"]
    assert crawl.feed_links(ATOM, "https://acme.test/atom.xml") == ["https://acme.test/posts/one", "https://acme.test/posts/three"]
    assert crawl.feed_links(b"<config><link>x</link></config>", "https://acme.test/c.xml") is None        # XML that is not a feed
    assert crawl.feed_links(b'<!DOCTYPE rss [<!ENTITY a "b">]><rss><channel/></rss>', "https://acme.test/f") is None
    assert crawl.feed_links(b"<rss><channel>", "https://acme.test/f") is None


async def test_a_feed_is_an_index_its_posts_are_ingested_even_at_depth_zero(tmp_path):
    site = FakeSite({"https://acme.test/feed.xml": (200, "application/rss+xml", RSS), "https://acme.test/posts/one": page(text="one"),
                     "https://acme.test/posts/two": page("/about", text="two"), "https://other.test/x": page()})
    found, problems = [], []
    async for x in crawl.crawl({"url": "https://acme.test/feed.xml", "robots": False, "max_depth": 0}, tmp_path / "web", Budget(), site):
        (found if isinstance(x, crawl.Found) else problems).append(x)
    assert [f.uri for f in found] == ["https://acme.test/posts/one", "https://acme.test/posts/two"] and not problems
    assert "https://other.test/x" not in site.asked and "https://acme.test/about" not in site.asked


# ---------------------------------------------------------------- llms-full.txt and llms.txt

FULL = (200, "text/plain; charset=utf-8", ("# Acme API\n\n" + PROSE).encode())
ONE_PAGE = page("https://api.acme.test/openapi.json", text="reference")


def test_links_of_an_llms_txt_index():
    text = ("# Acme\n\n> docs\n\n## Docs\n- [Auth](/docs/auth.md): how to sign in\n- [Search](https://acme.test/docs/search.md)\n"
            "- [Auth again](/docs/auth.md#top)\n- <https://acme.test/docs/raw.md>\nsee https://other.test/x.md, and [bad](javascript:x)\n")
    assert crawl.llms_links(text, "https://acme.test/llms.txt") == [
        "https://acme.test/docs/auth.md", "https://acme.test/docs/search.md", "https://acme.test/docs/raw.md", "https://other.test/x.md"]


async def test_a_site_of_one_page_is_asked_for_its_llms_full_txt(tmp_path):
    site = FakeSite({"https://acme.test/": ONE_PAGE, "https://acme.test/llms-full.txt": FULL,
                     "https://acme.test/llms.txt": (200, "text/plain", b"# Acme\n- [A](/a.md)\n" * 20)})
    found, problems = await run_crawl(tmp_path, site, robots=False)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/llms-full.txt"] and not problems
    assert found[1].path.suffix == ".txt" and b"Acme API" in found[1].path.read_bytes()
    assert "https://acme.test/llms.txt" not in site.asked


async def test_without_llms_full_txt_the_pages_llms_txt_lists_are_fetched_and_not_followed(tmp_path):
    index = b"# Acme\n\n> The Acme docs for language models, page by page.\n\n- [Auth](/docs/auth.md): signing in\n- [Guide](/guide)\n- [Out](https://other.test/x.md)\n" + b"\n" * 200
    site = FakeSite({"https://acme.test/": ONE_PAGE, "https://acme.test/llms.txt": (200, "text/markdown", index),
                     "https://acme.test/docs/auth.md": (200, "text/markdown", b"# Auth\n\nsend the key"),
                     "https://acme.test/guide": page("/guide/deeper", text="guide"), "https://acme.test/guide/deeper": page()})
    found, problems = await run_crawl(tmp_path, site, robots=False)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/llms.txt", "https://acme.test/docs/auth.md", "https://acme.test/guide"]
    assert not problems                                         # the missing llms-full.txt is not a failed page
    assert "https://acme.test/guide/deeper" not in site.asked and "https://other.test/x.md" not in site.asked


async def test_a_site_that_crawls_is_not_asked_for_llms_files(tmp_path):
    site = FakeSite({"https://acme.test/": page("/a"), "https://acme.test/a": page(), "https://acme.test/llms-full.txt": FULL})
    found, _ = await run_crawl(tmp_path, site, robots=False)
    assert [f.uri for f in found] == ["https://acme.test/", "https://acme.test/a"] and not any("llms" in u for u in site.asked)
    site = FakeSite({"https://acme.test/": ONE_PAGE, "https://acme.test/llms-full.txt": FULL})
    found, _ = await run_crawl(tmp_path, site, robots=False, max_pages=1)     # one page was asked for
    assert len(found) == 1 and not any("llms" in u for u in site.asked)


async def test_an_llms_file_the_page_links_to_on_its_parent_site_is_taken(tmp_path):
    site = FakeSite({"https://docs.acme.test/": page("https://acme.test/llms-full.txt", "https://other.test/llms-full.txt", text="reference"),
                     "https://acme.test/robots.txt": (200, "text/plain", b"User-agent: *\nAllow: /\n"),
                     "https://acme.test/llms-full.txt": FULL, "https://other.test/llms-full.txt": FULL})
    found, problems = [], []
    async for x in crawl.crawl({"url": "https://docs.acme.test/"}, tmp_path / "web", Budget(), site):
        (found if isinstance(x, crawl.Found) else problems).append(x)
    assert [f.uri for f in found] == ["https://docs.acme.test/", "https://acme.test/llms-full.txt"] and not problems
    assert "https://acme.test/robots.txt" in site.asked and not any("other.test" in u for u in site.asked)


async def test_an_llms_file_that_is_a_web_page_or_disallowed_is_not_kept(tmp_path):
    site = FakeSite({"https://acme.test/": ONE_PAGE, "https://acme.test/llms-full.txt": page(text="the app shell answers every path"),
                     "https://acme.test/llms.txt": (200, "text/plain", b"not found")})
    found, problems = await run_crawl(tmp_path, site, robots=False)
    assert [f.uri for f in found] == ["https://acme.test/"] and not problems
    site = FakeSite({"https://acme.test/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /llms\n"),
                     "https://acme.test/": ONE_PAGE, "https://acme.test/llms-full.txt": FULL})
    found, problems = await run_crawl(tmp_path, site)
    assert [f.uri for f in found] == ["https://acme.test/"] and not problems and "https://acme.test/llms-full.txt" not in site.asked


async def test_an_llms_txt_given_as_the_address_is_an_index(tmp_path):
    index = b"# Acme\n- [Auth](/docs/auth.md)\n"
    site = FakeSite({"https://acme.test/llms.txt": (200, "text/plain", index), "https://acme.test/docs/auth.md": (200, "text/markdown", b"# Auth")})
    found, problems = [], []
    async for x in crawl.crawl({"url": "https://acme.test/llms.txt", "robots": False}, tmp_path / "web", Budget(), site):
        (found if isinstance(x, crawl.Found) else problems).append(x)
    assert [f.uri for f in found] == ["https://acme.test/llms.txt", "https://acme.test/docs/auth.md"] and not problems


# ---- the same source twice, and tidying the queue

async def test_a_source_that_was_already_ingested_is_not_queued_again_without_a_word(client, conn, tmp_path, monkeypatch):
    site = FakeSite({"https://acme.test/": page(text="waf bypass notes")})
    monkeypatch.setattr(crawl, "Fetcher", lambda allow_local=False: site)
    from hunterai import worker
    cid = (await client.post("/api/kb/collections", json={"name": "Web", "embedder": "fake:64"})).json()["id"]
    other = (await client.post("/api/kb/collections", json={"name": "Other", "embedder": "fake:64"})).json()["id"]
    web = {"url": "https://acme.test/", "robots": False}
    first = (await client.post(f"/api/kb/collections/{cid}/ingest/web", json=web)).json()
    # Still waiting in the queue: a second one would only do the same work twice.
    r = await client.post(f"/api/kb/collections/{cid}/ingest/web", json={**web, "again": True})
    assert r.status_code == 409 and "is already in the queue" in r.json()["detail"]
    assert await worker.drain(conn) == 1
    r = await client.post(f"/api/kb/collections/{cid}/ingest/web", json=web)
    assert r.status_code == 409 and r.json()["detail"].startswith("Already ingested: https://acme.test/ was added to this collection on ")
    assert "1 file" in r.json()["detail"]
    assert len((await client.get(f"/api/kb/jobs?collection_id={cid}")).json()) == 1
    # Another collection has not seen it; and the owner may ask for it again to refresh it.
    assert (await client.post(f"/api/kb/collections/{other}/ingest/web", json=web)).status_code == 200
    again = await client.post(f"/api/kb/collections/{cid}/ingest/web", json={**web, "again": True})
    assert again.status_code == 200 and again.json()["id"] != first["id"]
    # The same for a repository and a folder.
    git = {"url": "https://github.com/o/r"}
    assert (await client.post(f"/api/kb/collections/{cid}/ingest/git", json=git)).status_code == 200
    assert (await client.post(f"/api/kb/collections/{cid}/ingest/git", json={"url": "https://github.com/o/r.git"})).status_code == 409
    folder = tmp_path / "notes"; folder.mkdir(); (folder / "a.md").write_text("hello")
    assert (await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(folder)})).status_code == 200
    assert (await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(folder) + "/"})).status_code == 409


async def test_finished_entries_of_the_queue_can_be_removed_and_duplicates_cleared(client, conn, tmp_path, monkeypatch):
    site = FakeSite({"https://acme.test/": page(text="waf bypass notes"), "https://b.test/": page(text="other")})
    monkeypatch.setattr(crawl, "Fetcher", lambda allow_local=False: site)
    from hunterai import worker
    cid = (await client.post("/api/kb/collections", json={"name": "Web", "embedder": "fake:64"})).json()["id"]
    ids = []
    for url, again in (("https://acme.test/", False), ("https://acme.test/", True), ("https://b.test/", False), ("https://acme.test/", True)):
        ids.append((await client.post(f"/api/kb/collections/{cid}/ingest/web", json={"url": url, "robots": False, "again": again})).json()["id"])
        if len(ids) < 4:
            assert await worker.drain(conn) == 1
    # The last one still waits: it cannot be removed, only cancelled; and it is not counted as a duplicate to clear.
    assert (await client.delete(f"/api/kb/jobs/{ids[3]}")).status_code == 409
    docs_before = (await client.get(f"/api/kb/collections/{cid}/documents")).json()["items"]
    r = await client.post(f"/api/kb/collections/{cid}/jobs/prune")
    assert r.json() == {"removed": 1}                                     # the older finished run of acme.test
    left = [j["id"] for j in (await client.get(f"/api/kb/jobs?collection_id={cid}")).json()]
    assert sorted(left) == sorted([ids[1], ids[2], ids[3]])
    assert (await client.post(f"/api/kb/collections/{cid}/jobs/prune")).json() == {"removed": 0}
    assert (await client.delete(f"/api/kb/jobs/{ids[2]}")).status_code == 204
    assert (await client.delete(f"/api/kb/jobs/{ids[2]}")).status_code == 404
    # Only the entries went: what was ingested is still there and still found.
    assert (await client.get(f"/api/kb/collections/{cid}/documents")).json()["items"] == docs_before
    hits = (await client.post("/api/kb/search", json={"collection_ids": [cid], "query": "waf bypass"})).json()["hits"]
    assert hits and hits[0]["source_uri"] == "https://acme.test/"
    # With its entry gone, b.test counts as new again... but its documents are known, so it is still named as ingested.
    r = await client.post(f"/api/kb/collections/{cid}/ingest/web", json={"url": "https://b.test/", "robots": False})
    assert r.status_code == 409 and r.json()["detail"].startswith("Already ingested")


async def test_what_a_clone_or_crawl_has_fetched_is_written_into_the_job(conn, tmp_path):
    from hunterai.kb import pipeline
    from hunterai.repo import jobs
    job = await jobs.enqueue(conn, "ingest", {"collection_id": "c", "source": {"kind": "git", "url": "https://github.com/o/r.git"}})
    root = tmp_path / "staging"; (root / "repo" / "sub").mkdir(parents=True)
    (root / "repo" / "a.bin").write_bytes(b"x" * 1000)
    watch = asyncio.create_task(pipeline.watch_fetch(conn, job.id, root, every=0.01))
    await asyncio.sleep(0.05)
    assert (await jobs.get(conn, job.id)).stats["fetched_bytes"] == 1000
    (root / "repo" / "sub" / "b.bin").write_bytes(b"y" * 500)
    await asyncio.sleep(0.05)
    assert (await jobs.get(conn, job.id)).stats["fetched_bytes"] == 1500
    watch.cancel()
    seen = []
    async def items():
        yield 1; yield 2
    still = asyncio.create_task(asyncio.sleep(30))
    async for x in pipeline._then(items(), still):
        seen.append(x)
    assert seen == [1, 2] and still.cancelled()


async def test_embedding_waits_for_a_hot_processor_and_says_so(conn, monkeypatch):
    from hunterai import thermal
    from hunterai.kb import pipeline
    from hunterai.repo import jobs
    monkeypatch.delenv("HUNTERAI_MAX_CPU_TEMP", raising=False)                  # the limit is 90
    job = await jobs.enqueue(conn, "ingest", {"collection_id": "c"})
    readings = iter([84.0])
    monkeypatch.setattr(thermal, "cpu_temp", lambda: next(readings))
    await pipeline.cool_down(conn, job.id, step=0)                               # cool: no wait, nothing written
    assert "cooling" not in (await jobs.get(conn, job.id)).stats
    seen, checks = [], []
    readings = iter([97.0, 95.0, 88.0, 85.0])                                   # under the limit is not enough: five degrees under

    def temp():
        t = next(readings)
        seen.append(t)
        return t

    async def check():
        checks.append((await jobs.get(conn, job.id)).stats.get("cooling"))
    monkeypatch.setattr(thermal, "cpu_temp", temp)
    await pipeline.cool_down(conn, job.id, check, step=0)
    assert seen == [97.0, 95.0, 88.0, 85.0] and checks == [97.0, 95.0, 88.0]     # the job said how hot it was while it waited
    assert (await jobs.get(conn, job.id)).stats["cooling"] is None
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 99.0)
    monkeypatch.setenv("HUNTERAI_MAX_CPU_TEMP", "off")                           # the owner switched the guard off
    await pipeline.cool_down(conn, job.id, step=0)
    monkeypatch.setattr(thermal, "cpu_temp", lambda: None)                       # a machine with no sensor is not held up
    monkeypatch.delenv("HUNTERAI_MAX_CPU_TEMP")
    await pipeline.cool_down(conn, job.id, step=0)


async def test_a_clone_whose_host_does_not_resolve_is_tried_once_more(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setattr(gitsrc, "RESOLVE_RETRY_PAUSE", 0)
    flaky = ("#!/bin/sh\nif [ ! -f " + str(tmp_path) + "/tried ]; then touch " + str(tmp_path) + "/tried; "
             "echo \"fatal: unable to access 'https://github.com/owner/repo.git/': Could not resolve host: github.com\" >&2; exit 128; fi\n"
             + FAKE_GIT.replace("$FAKE_OUT", str(tmp_path)).split("\n", 1)[1])
    fake_bin("git", flaky)
    found, problems = await collect(await git_payload())
    assert sorted(f.uri for f in found) == ["git:github.com/owner/repo/README.md", "git:github.com/owner/repo/src/scan.py"]
    fake_bin("git", "#!/bin/sh\necho 'fatal: Could not resolve host: github.com' >&2\nexit 128\n")      # twice: it is reported
    found, problems = await collect(await git_payload())
    assert found == [] and "Could not resolve host" in problems[0].reason


async def test_a_source_that_could_not_be_fetched_is_a_failed_job_with_its_reason(client, conn, fake_bin, monkeypatch, tmp_path):
    from hunterai import worker
    monkeypatch.setattr(gitsrc, "RESOLVE_RETRY_PAUSE", 0)
    fake_bin("git", "#!/bin/sh\necho 'fatal: Could not resolve host: github.com' >&2\nexit 128\n")
    cid = (await client.post("/api/kb/collections", json={"name": "Git", "embedder": "fake:64"})).json()["id"]
    job = (await client.post(f"/api/kb/collections/{cid}/ingest/git", json={"url": "https://github.com/o/r"})).json()
    await worker.drain(conn)
    got = (await client.get(f"/api/kb/jobs/{job['id']}")).json()
    assert got["state"] == "failed" and "Could not resolve host" in got["error"]
    # Not "already ingested": it never was. Retry runs the clone again, and now it works.
    fake_bin("git", FAKE_GIT.replace("$FAKE_OUT", str(tmp_path)))
    assert (await client.post(f"/api/kb/jobs/{job['id']}/retry")).status_code == 200
    await worker.drain(conn)
    got = (await client.get(f"/api/kb/jobs/{job['id']}")).json()
    assert got["state"] == "done" and got["stats"]["ok"] == 2


async def test_a_run_that_brought_nothing_in_does_not_count_as_ingested(client, conn):
    from hunterai.repo import jobs
    cid = (await client.post("/api/kb/collections", json={"name": "Git", "embedder": "fake:64"})).json()["id"]
    source = {"kind": "git", "url": "https://github.com/o/r.git"}
    old = await jobs.enqueue(conn, "ingest", {"collection_id": cid, "source": source})        # as jobs ended before: "done" with no file
    await jobs.claim(conn, ["ingest"])
    await jobs.progress(conn, old.id, set={"ok": 0, "unchanged": 0, "failed": 1})
    await jobs.finish(conn, old.id, "done")
    r = await client.post(f"/api/kb/collections/{cid}/ingest/git", json={"url": "https://github.com/o/r"})
    assert r.status_code == 200 and r.json()["id"] != old.id


async def test_a_helper_program_dies_with_the_worker_that_started_it(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    script = tmp_path / "parent.py"
    script.write_text(
        "import asyncio, sys\nfrom pathlib import Path\nfrom hunterai.kb import tools\n"
        f"asyncio.run(tools.run_tool(['sh', '-c', 'echo $$ > {tmp_path}/child.pid; exec sleep 60'], timeout=60, cwd=Path('{tmp_path}')))\n")
    parent = subprocess.Popen([sys.executable, str(script)], cwd=os.getcwd(), env={**os.environ, "PYTHONPATH": os.getcwd()})
    try:
        for _ in range(200):
            if (tmp_path / "child.pid").exists() and (tmp_path / "child.pid").read_text().strip():
                break
            await asyncio.sleep(0.05)
        child = int((tmp_path / "child.pid").read_text())
        os.kill(child, 0)                                   # it runs
        parent.send_signal(signal.SIGKILL)                  # the worker is killed outright: no clean-up of its own runs
        parent.wait()
        for _ in range(100):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            os.kill(child, signal.SIGKILL)
            raise AssertionError("the helper program outlived the worker")
    finally:
        if parent.poll() is None:
            parent.kill()
