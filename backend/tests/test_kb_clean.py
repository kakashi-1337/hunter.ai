"""scripts/kb-clean.sh: what would be left out of a collection today is taken out of it, and only when asked to."""
from hunterai import config
from hunterai.kb import clean, sources
from hunterai.kb.vectors import VectorError, get_store
from hunterai.repo import jobs, kb, settings
from tests.kb_samples import ingest_folder, make_collection, tree
from tests.test_kb_generated import BUILD, LOCK, LONG_LINES, MAP, MINIFIED, ONE_LINE, WRITTEN

GUIDE = b"# Guide\n\nThe zebra crossing rules of the site."
BUILT = b"<html><body><p>" + b"built page about the okapi enclosure. " * 20 + b"</p></body></html>"
SITE = {"src/app.js": WRITTEN, "docs/guide.md": GUIDE, ".next/static/main.js": ONE_LINE, ".next/server/page.html": BUILT,
        "public/jquery.min.js": ONE_LINE, "public/main-4f3a91.js": ONE_LINE, "public/app.js.map": b'{"version": 3, "sources": ["quagga.js"]}',
        "yarn.lock": b"# yarn lockfile v1\nlodash@4:\n  version 4\n"}
LEFT_OUT = {".next/static/main.js": BUILD, ".next/server/page.html": BUILD, "public/jquery.min.js": MINIFIED,
            "public/main-4f3a91.js": LONG_LINES, "public/app.js.map": MAP, "yarn.lock": LOCK}


async def filled(conn, col, tmp_path):
    """A collection that was filled before anything was left out."""
    root = tmp_path / "site"; tree(root, SITE)
    await settings.put(conn, "kb_skip_generated", False)
    state, _ = await ingest_folder(conn, col, root)
    await settings.put(conn, "kb_skip_generated", True)
    assert state == "done" and (await kb.counts(conn, col.id))["documents"]["ok"] == len(SITE)
    return root


async def state_of(conn, col, root) -> dict[str, tuple[str, str | None, int]]:
    _, docs = await kb.list_documents(conn, col.id, limit=500)
    return {d.source_uri.removeprefix(f"{root}/"): (d.status, d.reason, d.chunk_count) for d in docs}


def listed() -> str:
    files = sorted((config.home() / "kb-clean").glob("*.txt"))
    return files[-1].read_text() if files else ""


async def test_the_plan_names_what_would_be_left_out_today_and_why(conn, col, tmp_path):
    root = await filled(conn, col, tmp_path)
    before = await state_of(conn, col, root)
    entries = await clean.plan(conn, col)
    assert {e.document.source_uri.removeprefix(f"{root}/"): e.reason for e in entries} == LEFT_OUT
    assert all(e.passages == before[e.document.source_uri.removeprefix(f"{root}/")][2] > 0 for e in entries)


async def test_without_being_asked_to_it_changes_nothing(conn, col, tmp_path, capsys):
    root = await filled(conn, col, tmp_path); store, name = get_store(), kb.vector_name(col.id)
    before, vectors = await state_of(conn, col, root), await store.count(name)
    assert await clean.main([col.name]) == 0
    out = capsys.readouterr().out
    gone = sum(before[k][2] for k in LEFT_OUT)
    assert f"6 of these files would be left out: {gone} passages" in out and "Nothing was changed" in out
    assert f'scripts/kb-clean.sh "{col.name}" --apply' in out
    for why in (BUILD, MINIFIED, LONG_LINES, MAP, LOCK):
        assert why.removesuffix(" (not ingested; Settings can turn this off)") in out and "Settings can turn this off" not in out
    assert await state_of(conn, col, root) == before and await store.count(name) == vectors == sum(v[2] for v in before.values())
    assert all(f"{root}/{k}" in listed() for k in LEFT_OUT) and f"{root}/src/app.js" not in listed()


async def test_asked_to_it_takes_them_out_and_leaves_the_rest(conn, col, tmp_path, capsys):
    root = await filled(conn, col, tmp_path); store, name = get_store(), kb.vector_name(col.id)
    before = await state_of(conn, col, root)
    assert len(await kb.keyword_ids(conn, [col.id], '"okapi"', 10)) >= 1
    assert await clean.main([col.name, "--apply"]) == 0
    out = capsys.readouterr().out
    after = await state_of(conn, col, root)
    assert after == {**before, **{k: ("skipped", why, 0) for k, why in LEFT_OUT.items()}}            # each stays listed, with the reason
    kept = before["src/app.js"][2] + before["docs/guide.md"][2]
    assert (await kb.counts(conn, col.id))["chunks"] == await store.count(name) == kept
    assert await kb.keyword_ids(conn, [col.id], '"okapi"', 10) == [] and len(await kb.keyword_ids(conn, [col.id], '"zebra"', 10)) == 1
    assert f"Taken out of \"{col.name}\": 6 files, {sum(before[k][2] for k in LEFT_OUT)} passages" in out and f"{kept} passages" in out
    assert all(f"{root}/{k}" in listed() for k in LEFT_OUT)
    assert await clean.main([col.name, "--apply"]) == 0                                          # asked again: nothing is left to take out
    assert "nothing that would be left out" in capsys.readouterr().out and await state_of(conn, col, root) == after


async def test_what_was_taken_out_stays_out_and_comes_back_only_with_the_switch_off(conn, col, tmp_path, capsys):
    root = await filled(conn, col, tmp_path); store, name = get_store(), kb.vector_name(col.id)
    before = await state_of(conn, col, root)
    assert await clean.main([col.name, "--apply"]) == 0
    taken_out = await state_of(conn, col, root)
    await ingest_folder(conn, col, root)                             # the folder is indexed again, as indexing is set now
    again = await state_of(conn, col, root)
    assert {k: v for k, v in again.items() if not k.endswith("/")} == taken_out and again[".next/"] == ("skipped", BUILD, 0)
    await settings.put(conn, "kb_skip_generated", False)             # the owner wants them after all
    await ingest_folder(conn, col, root)
    back = await state_of(conn, col, root)
    assert {k: back[k] for k in before} == before and (await kb.counts(conn, col.id))["chunks"] == await store.count(name)


async def test_it_waits_for_the_collections_jobs(conn, col, tmp_path, capsys):
    root = await filled(conn, col, tmp_path)
    before = await state_of(conn, col, root)
    await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id),
                                        "source": {"kind": "folder", "path": str(root)}})
    assert await clean.main([col.name, "--apply"]) == 1
    assert "job" in capsys.readouterr().out and await state_of(conn, col, root) == before
    assert await clean.main([col.name]) == 0                     # looking is allowed meanwhile


async def test_with_the_switch_off_nothing_is_left_out_so_nothing_is_taken_out(conn, col, tmp_path, capsys):
    root = await filled(conn, col, tmp_path)
    before = await state_of(conn, col, root)
    await settings.put(conn, "kb_skip_generated", False)
    assert await clean.main([col.name, "--apply"]) == 1
    assert "switched off" in capsys.readouterr().out and await state_of(conn, col, root) == before


async def test_a_page_of_a_crawled_site_is_never_taken_out(conn, col, tmp_path):
    from hunterai.kb import chunking
    doc = await kb.begin_document(conn, col.id, "web", "https://example.com/static/app.min.js", "sha", 10, None,
                                  chunking.chunk(ONE_LINE.decode(), "code"))
    await kb.complete_document(conn, doc.id)
    assert await clean.plan(conn, col) == []


async def test_which_collection(conn, col, capsys):
    other = await make_collection(conn, "memories")
    await settings.put(conn, "memory_collection_id", other.id)
    assert await clean.main([]) == 0
    out = capsys.readouterr().out
    assert f'"{col.name}"' in out and "memories" not in out          # the memory collection is not managed here
    assert await clean.main(["no such"]) == 1 and f'"{col.name}"' in capsys.readouterr().out
    assert await clean.main(["memories", "--apply"]) == 1


async def test_a_vector_store_that_cannot_be_reached_stops_it_and_loses_nothing(conn, col, tmp_path, capsys, monkeypatch):
    root = await filled(conn, col, tmp_path)
    before = await state_of(conn, col, root)
    real = get_store()

    class Down:
        async def delete_by_document(self, name, document_id):
            raise VectorError("connection refused")

        def __getattr__(self, item):
            return getattr(real, item)
    monkeypatch.setattr(clean, "get_store", lambda: Down())
    assert await clean.main([col.name, "--apply"]) == 1
    assert "connection refused" in capsys.readouterr().out
    assert await state_of(conn, col, root) == before                 # a document goes whole or stays whole
