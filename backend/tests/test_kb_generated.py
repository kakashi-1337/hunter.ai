"""What nobody wrote is left out of a collection: the output of a build, a minified or bundled file, a source map,
a lock file. Told by the directory it stands in, by its name, or by its shape; one switch decides."""
import pytest

from hunterai import config
from hunterai.kb import generated, sources
from hunterai.kb.vectors import get_store
from hunterai.repo import kb, settings
from tests.kb_samples import collect, folder_payload, ingest_folder, reader, tree, zip_bytes

BUILD = "build output or tool cache (not ingested; Settings can turn this off)"
MAP = "source map (not ingested; Settings can turn this off)"
MINIFIED = "minified or bundled file (not ingested; Settings can turn this off)"
LONG_LINES = "minified: most of it stands in very long lines (not ingested; Settings can turn this off)"
LOCK = "lock file (not ingested; Settings can turn this off)"
ONE_LINE = b"!function(e,t){" + b"var a=e.b(t),c=a.d;" * 400 + b"}();"                 # what a minifier leaves: one line
WRITTEN = b"".join(b"export function step%d(value) {\n  return value + %d\n}\n\n" % (i, i) for i in range(40))


# ---- the rules

@pytest.mark.parametrize("name, why", [
    ("main.js.map", MAP), ("styles.css.map", MAP), ("index.mjs.map", MAP), ("types.d.ts.map", MAP), ("APP.JS.MAP", MAP),
    ("jquery.min.js", MINIFIED), ("bootstrap.min.css", MINIFIED), ("yui-min.js", MINIFIED), ("vendor.bundle.js", MINIFIED),
    ("2.8a2b91f3.chunk.js", MINIFIED), ("main.4f3a.chunk.css", MINIFIED), ("lib.min.mjs", MINIFIED),
    ("package-lock.json", LOCK), ("yarn.lock", LOCK), ("pnpm-lock.yaml", LOCK), ("Cargo.lock", LOCK), ("poetry.lock", LOCK),
    ("go.sum", LOCK), ("composer.lock", LOCK), ("Gemfile.lock", LOCK), ("uv.lock", LOCK),
])
def test_a_name_that_says_machine_made(name, why):
    assert generated.by_name(name) == why


@pytest.mark.parametrize("name", [
    "app.js", "admin.js", "min.js", "bundle.js", "chunk.js", "map.js", "site.map", "roadmap.md", "sitemap.xml", "package.json",
    "lock.md", "unlock.py", "minify.py", "summary.min.md", "notes.txt", "README", "Dockerfile", "go.mod", "yarn.lock.md",
])
def test_a_name_that_says_nothing(name):
    assert generated.by_name(name) is None


def test_a_script_of_very_long_lines_is_minified_whatever_its_name():
    assert generated.by_shape("main-4f3a91.js", [ONE_LINE]) == LONG_LINES
    assert generated.by_shape("index-91ab.css", [b".a{color:red}" * 900]) == LONG_LINES
    assert generated.by_shape("worker.mjs", [b"/*! licence\n * kept\n */\n" + ONE_LINE]) == LONG_LINES   # a short header, then the line
    assert generated.by_shape("app.js", [WRITTEN]) is None
    assert generated.by_shape("app.js", [WRITTEN + b"const ICON = '" + b"A" * 600 + b"'\n"]) is None       # one long line in a written file


def test_a_small_file_with_one_long_line_is_not_what_a_minifier_made():
    """Seen in real packages: a script of five lines, one of them 520 characters. It makes one passage and harms nothing."""
    small = b"var x=String,\n" + b"create=function(){return {" + b"bold:x," * 75 + b"}};\n" + b"module.exports=create()\n"
    assert 500 < max(len(line) for line in small.split(b"\n")) < 600 and generated.by_shape("colors.browser.js", [small]) is None
    assert generated.by_shape("colors.browser.js", [small.replace(b"bold:x," * 75, b"bold:x," * 400)]) == LONG_LINES


def test_shape_is_asked_of_scripts_and_style_sheets_only():
    """A paragraph of prose is one long line, and so is a row of a table: neither is what a minifier made."""
    prose = b"Start of the paragraph. " + b"A sentence that goes on for a while. " * 60
    for name in ("notes.md", "notes.txt", "page.html", "data.json", "rows.csv", "query.sql", "main.py", "x.ts"):
        assert generated.by_shape(name, [prose]) is None
    assert generated.by_shape("notes.js", [prose]) == LONG_LINES


def test_shape_is_the_same_question_for_stored_passages():
    """A document that already stands in a collection is there as its passages. Each is a piece of the file."""
    assert generated.by_shape("main.js", [ONE_LINE[i:i + 1500] for i in range(0, len(ONE_LINE), 1500)]) == LONG_LINES
    assert generated.by_shape("main.js", [WRITTEN[i:i + 1500] for i in range(0, len(WRITTEN), 1500)]) is None
    assert generated.by_shape("main.js", []) is None


@pytest.mark.parametrize("kind, uri, why", [
    ("folder", "/home/u/web/.next/static/chunks/a.js", BUILD),
    ("folder", "/home/u/web/.yarn/releases/yarn-4.cjs", BUILD),
    ("folder", "/home/u/web/public/jquery.min.js", MINIFIED),
    ("folder", "/home/u/web/public/app.js.map", MAP),
    ("folder", "/home/u/web/yarn.lock", LOCK),
    ("git", "git:github.com/o/r/.nuxt/dist/a.js", BUILD),
    ("git", "git:github.com/o/r/assets/app.bundle.js", MINIFIED),
    ("upload", "upload:site/.svelte-kit/x.js", BUILD),
    ("upload", "upload:a.min.js", MINIFIED),
    ("folder", "/x/pkg.tgz!/package/.next/a.js", BUILD),             # a member of an archive, under a build directory
    ("folder", "/x/app.bundle.js.gz!/app.bundle.js", MINIFIED),      # by the name of the member
    ("folder", "/x/.cache/pkg.zip!/readme.md", BUILD),               # the archive itself stood in a cache
    ("git", "git:github.com/o/r/a.zip!/b.tar!/.turbo/c.md", BUILD),
])
def test_a_stored_document_that_would_be_left_out_today(kind, uri, why):
    assert generated.of_uri(kind, uri) == why


@pytest.mark.parametrize("kind, uri", [
    ("folder", "/home/u/web/src/a.js"), ("folder", "/home/u/web/dist/a.js"), ("folder", "/home/u/web/build/a.js"),
    ("folder", "/home/u/next/a.md"), ("folder", "/home/u/web/cache/a.md"),
    ("git", "git:github.com/o/r/README.md"),
    ("git", "git:github.com/o/.cache/README.md"),                    # a repository of that name is nobody's cache
    ("upload", "upload:notes.md"),
    ("folder", "/x/app.bundle.js.gz!/readme.md"),                    # the archive's own name is not its member's
    ("web", "https://example.com/.next/yarn.lock"),                  # a page somebody asked for is never left out
    ("web", "https://example.com/static/app.min.js"),
])
def test_a_stored_document_that_stays(kind, uri):
    assert generated.of_uri(kind, uri) is None


# ---- discovery

def site() -> dict[str, bytes]:
    return {"src/app.js": WRITTEN, "docs/guide.md": b"# Guide\n\nHow it is set up.", "package.json": b'{"name": "site"}',
            ".next/static/chunks/main.js": ONE_LINE, ".next/server/pages/index.html": b"<html>built</html>", ".cache/x.txt": b"cached",
            "public/jquery.min.js": ONE_LINE, "public/app.js.map": b'{"version": 3}', "yarn.lock": b"# yarn lockfile v1\n"}


async def test_a_folder_walk_leaves_out_what_nobody_wrote(tmp_path):
    root = tmp_path / "site"; tree(root, site())
    found, problems = await collect(folder_payload(root))
    assert {f.uri for f in found} == {f"{root}/src/app.js", f"{root}/docs/guide.md", f"{root}/package.json"}
    assert {(q.uri, q.status, q.reason) for q in problems} == {
        (f"{root}/.next/", "skipped", BUILD), (f"{root}/.cache/", "skipped", BUILD), (f"{root}/public/jquery.min.js", "skipped", MINIFIED),
        (f"{root}/public/app.js.map", "skipped", MAP), (f"{root}/yarn.lock", "skipped", LOCK)}


async def test_with_the_switch_off_a_walk_takes_everything(tmp_path):
    root = tmp_path / "site"; tree(root, {**site(), "node_modules/x.js": b"n", ".env": b"KEY=1"})
    found, problems = [], []
    async for x in sources.discover(folder_payload(root), skip_generated=False):
        (found if hasattr(x, "path") else problems).append(x)
    assert {f.uri.removeprefix(f"{root}/") for f in found} == set(site())
    assert {(q.uri.removeprefix(f"{root}/"), q.reason) for q in problems} == {                    # what is never read stays unread
        ("node_modules/", "ignored directory"), (".env", "secret-like file name (not ingested)")}


async def test_a_file_that_is_left_out_does_not_use_up_the_job(tmp_path, monkeypatch):
    from hunterai.kb import limits
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 1000); root = tmp_path / "site"
    tree(root, {"big.min.js": b"x" * 5000, "a.md": b"# a"})
    found, problems = await collect(folder_payload(root))
    assert [f.uri for f in found] == [f"{root}/a.md"] and [(q.uri, q.reason) for q in problems] == [(f"{root}/big.min.js", MINIFIED)]


async def test_members_of_an_archive_follow_the_same_rules(tmp_path):
    root = tmp_path / "src"
    tree(root, {"site.zip": zip_bytes({"src/main.py": b"print(1)", ".next/a/x.js": b"1", ".next/b/y.js": b"2", "pub/lib.min.js": b"m",
                                       "pub/lib.js.map": b"{}", "Cargo.lock": b"l"})})
    found, problems = await collect(folder_payload(root))
    assert [f.uri for f in found] == [f"{root}/site.zip!/src/main.py"]
    assert {(q.uri.removeprefix(f"{root}/site.zip!/"), q.reason) for q in problems} == {
        (".next/", BUILD), ("pub/lib.min.js", MINIFIED), ("pub/lib.js.map", MAP), ("Cargo.lock", LOCK)}
    left = {p.name for p in config.staging_dir().rglob("*") if p.is_file()} - {"meta.json"}
    assert left == {"main.py"}                                   # what is left out does not stay unpacked on disk either
    everything = [x async for x in sources.discover(folder_payload(root), skip_generated=False)]
    assert len(everything) == 6 and all(hasattr(x, "path") for x in everything)


async def test_an_upload_follows_the_same_rules():
    sid = sources.new_staging("col1")
    for rel, data in {"site/.nuxt/x.js": b"1", "site/notes.md": b"# n", "app.min.js": b"m"}.items():
        await sources.stage_file(sid, rel, reader(data))
    found, problems = await collect({"collection_id": "col1", "staging_id": sid, "source": {"kind": "upload"}})
    assert [f.uri for f in found] == ["upload:site/notes.md"]
    assert {(q.uri, q.reason) for q in problems} == {("upload:site/.nuxt/", BUILD), ("upload:app.min.js", MINIFIED)}


# ---- a job

async def state_of(conn, col, root) -> dict[str, tuple[str, str | None, int]]:
    _, docs = await kb.list_documents(conn, col.id, limit=500)
    return {d.source_uri.removeprefix(f"{root}/"): (d.status, d.reason, d.chunk_count) for d in docs}


async def test_a_job_leaves_out_what_nobody_wrote_and_says_why(conn, col, tmp_path):
    root = tmp_path / "site"; store, name = get_store(), kb.vector_name(col.id)
    tree(root, {**site(), "public/main-4f3a91.js": ONE_LINE})                        # minified, and nothing in its name says so
    state, job = await ingest_folder(conn, col, root)
    docs = await state_of(conn, col, root)
    assert state == "done" and {k: v[:2] for k, v in docs.items()} == {
        "src/app.js": ("ok", None), "docs/guide.md": ("ok", None), "package.json": ("ok", None),
        ".next/": ("skipped", BUILD), ".cache/": ("skipped", BUILD), "public/jquery.min.js": ("skipped", MINIFIED),
        "public/app.js.map": ("skipped", MAP), "yarn.lock": ("skipped", LOCK), "public/main-4f3a91.js": ("skipped", LONG_LINES)}
    assert (job.stats["ok"], job.stats["skipped"], job.stats["failed"]) == (3, 6, 0)
    kept = sum(v[2] for v in docs.values())
    assert kept == (await kb.counts(conn, col.id))["chunks"] == await store.count(name) and all(v[2] == 0 for v in docs.values() if v[0] != "ok")


async def test_with_the_switch_off_a_job_indexes_them(conn, col, tmp_path):
    await settings.put(conn, "kb_skip_generated", False)
    root = tmp_path / "site"; tree(root, {**site(), "public/main-4f3a91.js": ONE_LINE})
    state, job = await ingest_folder(conn, col, root)
    docs = await state_of(conn, col, root)
    assert state == "done" and set(docs) == {*site(), "public/main-4f3a91.js"} and all(v[0] == "ok" for v in docs.values())


async def test_indexing_a_folder_again_takes_out_the_files_it_now_leaves_out(conn, col, tmp_path):
    """What was indexed before the rule: a file named or shaped like a minifier's work is set aside when its folder is
    indexed again, although it did not change."""
    root = tmp_path / "site"; store, name = get_store(), kb.vector_name(col.id)
    tree(root, {"src/app.js": WRITTEN, "public/jquery.min.js": ONE_LINE, "public/main-4f3a91.js": ONE_LINE})
    await settings.put(conn, "kb_skip_generated", False)
    await ingest_folder(conn, col, root)
    before = await state_of(conn, col, root)
    assert all(v[0] == "ok" and v[2] > 0 for v in before.values())
    await settings.put(conn, "kb_skip_generated", True)
    state, job = await ingest_folder(conn, col, root)
    after = await state_of(conn, col, root)
    assert state == "done" and after == {"src/app.js": before["src/app.js"], "public/jquery.min.js": ("skipped", MINIFIED, 0),
                                         "public/main-4f3a91.js": ("skipped", LONG_LINES, 0)}
    assert (await kb.counts(conn, col.id))["chunks"] == await store.count(name) == before["src/app.js"][2]
    assert (job.stats["unchanged"], job.stats["skipped"]) == (1, 2)


async def test_a_page_of_a_crawled_site_is_never_left_out(conn, col, tmp_path, monkeypatch):
    """Somebody asked for that page. Only the fetching is replaced here: the job is the real one."""
    from hunterai.kb import crawl, pipeline
    from hunterai.repo import jobs

    async def pages(source, dest, budget):
        tree(dest, {"p.js": ONE_LINE})
        yield sources.Found("https://example.com/p.js", dest / "p.js", len(ONE_LINE))
    monkeypatch.setattr(crawl, "crawl", pages)
    await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id),
                                        "source": {"kind": "web", "url": "https://example.com/"}})
    state = await pipeline.run_job(conn, await jobs.claim(conn, ["ingest"]))
    doc = await kb.find_document(conn, col.id, "https://example.com/p.js")
    assert state == "done" and (doc.status, doc.reason) == ("ok", None) and doc.chunk_count > 0
