"""The collections every chat reads unless the owner chose its collections himself, so that the agent can search
the knowledge base in a chat nobody attached one to: the new ones, and the ones that exist."""
from hunterai import memory
from hunterai.repo import kb, settings
from tests.kb_samples import make_collection
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)

URL = "/api/kb/new-chats"


async def attached(conn, chat) -> list[str]:
    return (await kb.get_chat_kb(conn, chat["id"])).collection_ids


async def test_a_new_chat_has_no_collection_until_the_owner_names_one_for_every_chat(client, conn):
    await make_collection(conn, "RAG")
    assert (await client.get(URL)).json() == {"collection_ids": []}
    assert await attached(conn, await new_chat(client)) == []


async def test_every_new_chat_starts_with_the_collections_the_owner_named(client, conn):
    rag, notes = await make_collection(conn, "RAG"), await make_collection(conn, "Notes")
    before = await new_chat(client)
    assert (await client.put(URL, json={"collection_ids": [rag.id]})).json() == {"collection_ids": [rag.id]}
    assert (await client.get(URL)).json() == {"collection_ids": [rag.id]}
    assert await attached(conn, await new_chat(client)) == [rag.id]
    assert await attached(conn, before) == [rag.id]                             # a chat that exists reads them too
    said = (await client.put(URL, json={"collection_ids": [rag.id, notes.id, rag.id]})).json()
    assert said == {"collection_ids": [notes.id, rag.id]}                       # each once, by name as a chat lists them
    assert await attached(conn, await new_chat(client)) == [notes.id, rag.id]
    assert (await client.put(URL, json={"collection_ids": []})).json() == {"collection_ids": []}
    assert await attached(conn, await new_chat(client)) == []


async def test_the_collections_of_a_persona_stay_and_the_named_ones_are_added(client, conn):
    rag, re_notes = await make_collection(conn, "RAG"), await make_collection(conn, "RE notes")
    p = (await client.post("/api/personas", json={"name": "RE", "system_prompt": "x", "collections": [re_notes.id]})).json()
    await client.put(URL, json={"collection_ids": [rag.id]})
    assert await attached(conn, await new_chat(client, persona_id=p["id"])) == [rag.id, re_notes.id]
    # Only a new chat: giving a chat that exists a persona does not bring the named ones along.
    old = await new_chat(client)
    await client.put(f"/api/kb/chats/{old['id']}", json={"collection_ids": []})
    await client.patch(f"/api/chats/{old['id']}", json={"persona_id": p["id"]})
    assert await attached(conn, old) == [re_notes.id]


async def test_a_chat_the_owner_emptied_stays_empty(client, conn):
    rag = await make_collection(conn, "RAG")
    await client.put(URL, json={"collection_ids": [rag.id]})
    chat = await new_chat(client)
    await client.put(f"/api/kb/chats/{chat['id']}", json={"collection_ids": []})
    await client.patch(f"/api/chats/{chat['id']}", json={"title": "renamed"})
    assert await attached(conn, chat) == []


async def test_a_collection_that_does_not_exist_is_not_named_and_one_that_is_gone_is_dropped(client, conn):
    rag, notes = await make_collection(conn, "RAG"), await make_collection(conn, "Notes")
    assert (await client.put(URL, json={"collection_ids": [rag.id, "nope"]})).status_code == 404
    assert (await client.get(URL)).json() == {"collection_ids": []}             # nothing of a refused list is kept
    await client.put(URL, json={"collection_ids": [rag.id, notes.id]})
    assert (await client.delete(f"/api/kb/collections/{notes.id}")).status_code == 204
    assert (await client.get(URL)).json() == {"collection_ids": [rag.id]}
    assert await attached(conn, await new_chat(client)) == [rag.id]


async def test_the_memory_collection_is_not_one_for_every_chat(client, conn):
    await settings.put(conn, "memory_embedder", "fake:64")
    await memory.add(conn, "acme rotates keys weekly.")
    hidden = await memory.collection(conn)
    assert (await client.put(URL, json={"collection_ids": [hidden.id]})).status_code == 404


async def test_a_chat_nobody_chose_collections_for_reads_what_the_owner_names_whenever_he_names_it(client, conn):
    """What this is for was counted on the owner's own install: 48 of his 49 chats read no collection, none was
    ticked "every new chat", and every search of the agent was answered "No knowledge collection is attached"."""
    rag, notes = await make_collection(conn, "RAG"), await make_collection(conn, "Notes")
    old = await new_chat(client)
    said = (await client.get(f"/api/kb/chats/{old['id']}")).json()
    assert (said["collection_ids"], said["follows"]) == ([], True)
    await client.put(URL, json={"collection_ids": [rag.id]})
    assert await attached(conn, old) == [rag.id] and (await kb.get_chat_kb(conn, old["id"])).follows
    await client.put(URL, json={"collection_ids": [rag.id, notes.id]})
    assert await attached(conn, old) == [notes.id, rag.id]                      # by name, as a chat lists its own
    await client.put(URL, json={"collection_ids": []})
    assert await attached(conn, old) == [] and await attached(conn, await new_chat(client)) == []


async def test_a_chat_with_collections_of_its_own_keeps_them_whatever_is_named(client, conn):
    rag, notes = await make_collection(conn, "RAG"), await make_collection(conn, "Notes")
    chat = await new_chat(client)
    said = (await client.put(f"/api/kb/chats/{chat['id']}", json={"collection_ids": [notes.id]})).json()
    assert (said["collection_ids"], said["follows"]) == ([notes.id], False)
    await client.put(URL, json={"collection_ids": [rag.id]})
    assert await attached(conn, chat) == [notes.id]


async def test_detaching_one_in_a_chat_that_follows_makes_the_rest_its_own(client, conn):
    rag, notes, late = await make_collection(conn, "RAG"), await make_collection(conn, "Notes"), await make_collection(conn, "Late")
    await client.put(URL, json={"collection_ids": [rag.id, notes.id]})
    chat = await new_chat(client)
    assert await attached(conn, chat) == [notes.id, rag.id]
    said = (await client.put(f"/api/kb/chats/{chat['id']}", json={"collection_ids": [rag.id]})).json()
    assert (said["collection_ids"], said["follows"]) == ([rag.id], False)
    await client.put(URL, json={"collection_ids": [rag.id, notes.id, late.id]})
    assert await attached(conn, chat) == [rag.id]                               # his choice for this chat stands


async def test_the_retrieve_switch_of_a_chat_that_follows_leaves_it_following(client, conn):
    """The switch and the collections are kept in the same place: setting one must not read as a choice of the other."""
    rag = await make_collection(conn, "RAG")
    chat = await new_chat(client)
    said = (await client.put(f"/api/kb/chats/{chat['id']}", json={"always_retrieve": False})).json()
    assert (said["collection_ids"], said["follows"], said["always_retrieve"]) == ([], True, False)
    await client.put(URL, json={"collection_ids": [rag.id]})
    said = (await client.get(f"/api/kb/chats/{chat['id']}")).json()
    assert (said["collection_ids"], said["follows"], said["always_retrieve"]) == ([rag.id], True, False)


async def test_an_emptied_chat_that_gets_a_collection_again_is_no_longer_marked_emptied(client, conn):
    rag, notes = await make_collection(conn, "RAG"), await make_collection(conn, "Notes")
    await client.put(URL, json={"collection_ids": [rag.id]})
    chat = await new_chat(client)
    await client.put(f"/api/kb/chats/{chat['id']}", json={"collection_ids": []})
    assert await attached(conn, chat) == [] and not (await kb.get_chat_kb(conn, chat["id"])).follows
    await client.put(f"/api/kb/chats/{chat['id']}", json={"collection_ids": [notes.id]})
    assert await attached(conn, chat) == [notes.id]
    await client.delete(f"/api/kb/collections/{notes.id}")                      # its only one is deleted, not detached:
    assert await attached(conn, chat) == [rag.id]                               # nobody emptied it, so it follows again


async def test_a_chat_started_while_a_collection_is_named_follows_what_is_named_later(client, conn):
    """Nothing is copied into a new chat: a copy would be a choice nobody made, and would keep the chat from following."""
    rag, notes = await make_collection(conn, "RAG"), await make_collection(conn, "Notes")
    await client.put(URL, json={"collection_ids": [rag.id]})
    chat = await new_chat(client)
    said = (await client.get(f"/api/kb/chats/{chat['id']}")).json()
    assert (said["collection_ids"], said["follows"]) == ([rag.id], True)
    await client.put(URL, json={"collection_ids": [notes.id]})
    assert await attached(conn, chat) == [notes.id]
