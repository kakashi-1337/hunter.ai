"""A document that was read from a folder of this machine, and whose file is gone from it, can be found and taken out."""
import shutil

from hunterai import worker
from hunterai.kb.vectors import get_store
from hunterai.repo import jobs, kb
from tests.test_api import client  # noqa: F401
from tests.test_kb_index_editor import search, setup


async def gone(client, cid):
    answer = await client.get(f"/api/kb/collections/{cid}/gone")
    assert answer.status_code == 200, answer.text
    return answer.json()


async def remove(client, cid):
    return await client.post(f"/api/kb/collections/{cid}/gone/remove")


async def test_nothing_is_gone_while_every_file_is_there(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path)
    assert await gone(client, cid) == {"checked": 2, "gone": 0, "first": [], "away": 0, "sources": [], "unknown": 0}
    assert (await remove(client, cid)).json() == {"removed": 0}


async def test_a_document_whose_file_was_deleted_is_found_and_can_be_taken_out(client, conn, tmp_path):
    cid, docs = await setup(client, conn, tmp_path)
    (tmp_path / "src" / "b.txt").unlink()
    found = await gone(client, cid)
    assert (found["checked"], found["gone"], found["first"], found["away"], found["unknown"]) == (2, 1, [str(tmp_path / "src" / "b.txt")], 0, 0)
    assert await search(client, cid, "beta payload two") != []                # looking takes nothing out
    taken = await remove(client, cid)
    assert taken.status_code == 200 and taken.json() == {"removed": 1}
    assert await kb.get_document(conn, docs["b.txt"]["id"]) is None and await kb.get_document(conn, docs["a.txt"]["id"]) is not None
    assert await get_store().count(kb.vector_name(cid)) == 1
    assert "beta payload two" not in await search(client, cid, "beta payload two")
    assert (await gone(client, cid))["gone"] == 0 and (await remove(client, cid)).json() == {"removed": 0}


async def test_the_files_of_a_folder_that_was_deleted_inside_its_source_are_gone(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path, {"keep.txt": b"kept text here", "old/x.txt": b"old x text", "old/y.txt": b"old y text"})
    shutil.rmtree(tmp_path / "src" / "old")
    found = await gone(client, cid)
    assert (found["checked"], found["gone"]) == (3, 2)
    assert sorted(found["first"]) == [str(tmp_path / "src" / "old" / "x.txt"), str(tmp_path / "src" / "old" / "y.txt")]


async def test_a_source_that_is_not_there_is_left_alone(client, conn, tmp_path):
    cid, docs = await setup(client, conn, tmp_path)
    (tmp_path / "src").rename(tmp_path / "moved")                             # a drive that is not plugged in looks the same
    found = await gone(client, cid)
    assert (found["checked"], found["gone"], found["first"], found["away"], found["sources"]) == (2, 0, [], 2, [str(tmp_path / "src")])
    assert (await remove(client, cid)).json() == {"removed": 0}
    assert await kb.get_document(conn, docs["a.txt"]["id"]) is not None and await get_store().count(kb.vector_name(cid)) == 2


async def test_a_document_whose_folder_is_no_longer_in_the_queue_is_not_judged(client, conn, tmp_path):
    cid, docs = await setup(client, conn, tmp_path)
    for job in await jobs.list(conn, kind="ingest", limit=None):
        await jobs.delete(conn, job.id)
    (tmp_path / "src" / "b.txt").unlink()
    found = await gone(client, cid)
    assert (found["checked"], found["gone"], found["away"], found["unknown"]) == (2, 0, 0, 2)
    assert (await remove(client, cid)).json() == {"removed": 0} and await kb.get_document(conn, docs["b.txt"]["id"]) is not None


async def test_only_what_was_read_from_a_folder_of_this_machine_is_looked_at(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path)
    await client.post(f"/api/kb/collections/{cid}/notes", json={"title": "A note", "text": "a note of my own"})
    await kb.record_document(conn, cid, "git", "git:github.com/a/b/README.md", status="skipped", reason="for the test")
    assert (await gone(client, cid))["checked"] == 2


async def test_a_folder_named_like_the_start_of_another_is_not_its_source(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path)                              # the source is .../src
    other = tmp_path / "src-old"
    other.mkdir()
    await kb.record_document(conn, cid, "folder", str(other / "gone.txt"), status="skipped", reason="for the test")
    found = await gone(client, cid)
    assert (found["checked"], found["gone"], found["unknown"]) == (3, 0, 1)


async def test_the_member_of_an_archive_is_gone_with_its_archive(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path)
    await kb.record_document(conn, cid, "folder", str(tmp_path / "src" / "a.txt") + "!/inner/one.md", status="skipped", reason="for the test")
    await kb.record_document(conn, cid, "folder", str(tmp_path / "src" / "pack.zip") + "!/inner/two.md", status="skipped", reason="for the test")
    found = await gone(client, cid)
    assert (found["checked"], found["gone"], found["first"]) == (4, 1, [str(tmp_path / "src" / "pack.zip") + "!/inner/two.md"])


async def test_the_first_twenty_are_named_and_all_are_counted(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path, {f"f{i:02}.txt": f"text number {i}".encode() for i in range(23)})
    for i in range(22):
        (tmp_path / "src" / f"f{i:02}.txt").unlink()
    found = await gone(client, cid)
    assert (found["checked"], found["gone"], len(found["first"])) == (23, 22, 20)
    assert found["first"] == sorted(found["first"]) and found["first"][0].endswith("f00.txt")
    assert (await remove(client, cid)).json() == {"removed": 22}


async def test_nothing_is_taken_out_while_the_collection_is_being_ingested(client, conn, tmp_path):
    cid, docs = await setup(client, conn, tmp_path)
    (tmp_path / "src" / "b.txt").unlink()
    await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src"), "again": True})
    refused = await remove(client, cid)
    assert refused.status_code == 409 and await kb.get_document(conn, docs["b.txt"]["id"]) is not None


async def test_a_collection_that_is_not_there_is_not_found(client):
    assert (await client.get("/api/kb/collections/nope/gone")).status_code == 404
    assert (await client.post("/api/kb/collections/nope/gone/remove")).status_code == 404


async def test_a_folder_that_was_added_by_itself_too_and_then_deleted_is_gone_while_the_folder_around_it_is_there(client, conn, tmp_path):
    cid, _ = await setup(client, conn, tmp_path, {"keep.txt": b"kept text here", "old/x.txt": b"old x text"})
    await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(tmp_path / "src" / "old")})
    await worker.drain(conn)
    shutil.rmtree(tmp_path / "src" / "old")
    found = await gone(client, cid)
    assert (found["checked"], found["gone"], found["away"], found["first"]) == (2, 1, 0, [str(tmp_path / "src" / "old" / "x.txt")])
    (tmp_path / "src").rename(tmp_path / "moved")                             # now neither of its sources is there
    found = await gone(client, cid)
    assert (found["gone"], found["away"], found["sources"]) == (0, 2, [str(tmp_path / "src")])


async def test_a_folder_that_only_another_collection_has_in_its_queue_is_no_source_here(client, conn, tmp_path):
    await setup(client, conn, tmp_path)
    other = (await client.post("/api/kb/collections", json={"name": "Other", "embedder": "fake:64"})).json()["id"]
    await kb.record_document(conn, other, "folder", str(tmp_path / "src" / "never.txt"), status="skipped", reason="for the test")
    found = await gone(client, other)
    assert (found["checked"], found["gone"], found["unknown"]) == (1, 0, 1)


async def test_a_file_that_was_added_by_itself_is_its_own_source(client, conn, tmp_path):
    cid = (await client.post("/api/kb/collections", json={"name": "One", "embedder": "fake:64"})).json()["id"]
    one = tmp_path / "one.txt"
    one.write_bytes(b"a file of its own")
    await client.post(f"/api/kb/collections/{cid}/ingest/folder", json={"path": str(one)})
    await worker.drain(conn)
    assert await gone(client, cid) == {"checked": 1, "gone": 0, "first": [], "away": 0, "sources": [], "unknown": 0}
    one.unlink()
    found = await gone(client, cid)
    assert (found["gone"], found["away"], found["sources"], found["unknown"]) == (0, 1, [str(one)], 0)
