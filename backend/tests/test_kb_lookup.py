"""The knowledge base is looked up before every answer of a chat that reads a collection, unless the owner switched
that off: for one chat (the switch above the message box) or for every chat he did not choose for (Settings)."""
import pytest

from hunterai import db
from hunterai.repo import chats, kb, messages, settings
from hunterai.types import TextBlock
from tests.kb_samples import ingest_texts, make_collection
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)
from tests.test_engine import Scripted, go, use


@pytest.fixture
def chat_with(conn, tmp_path):
    """A chat that reads one collection with a note in it. Nobody touched its switch."""
    async def make(text="waf bypass notes"):
        col = await make_collection(conn, f"col{len(await kb.list_collections(conn))}")
        await ingest_texts(conn, col, tmp_path, {"notes.md": "waf bypass notes"})
        chat = await chats.create(conn, None, "pentesting", "abliteration", "abliterated-model", {})
        await kb.set_chat_kb(conn, chat.id, collection_ids=[col.id])
        return chat, await messages.add(conn, chat.id, None, "user", [TextBlock(text=text)]), col
    return make


def looked_up(run) -> bool:
    return any(s.kind == "retrieval" and s.name == "kb_search" for s in run.steps)


async def test_a_chat_that_reads_a_collection_looks_it_up_before_every_answer_from_the_start(conn, chat_with):
    p = Scripted("abliteration"); use(abliteration=p)
    chat, user, _ = await chat_with()
    assert (await kb.get_chat_kb(conn, chat.id)).always_retrieve is True
    run, msg = await go(conn, chat, user)
    assert looked_up(run) and msg.blocks[-1].type == "sources" and "KB_REFERENCE" in p.calls[0]["system"]


async def test_the_owner_switches_the_lookup_off_for_one_chat_and_on_again(conn, chat_with):
    use(abliteration=Scripted("abliteration"))
    chat, user, _ = await chat_with()
    assert (await kb.set_chat_kb(conn, chat.id, always_retrieve=False)).always_retrieve is False
    run, msg = await go(conn, chat, user)
    assert not looked_up(run) and all(b.type != "sources" for b in msg.blocks)
    assert (await kb.set_chat_kb(conn, chat.id, always_retrieve=True)).always_retrieve is True
    user2 = await messages.add(conn, chat.id, msg.id, "user", [TextBlock(text="waf bypass notes")])
    assert looked_up((await go(conn, chat, user2))[0])


async def test_the_owner_switches_the_lookup_off_for_every_chat_he_did_not_choose_for(conn, chat_with):
    use(abliteration=Scripted("abliteration"))
    untouched, user, _ = await chat_with()
    chosen, user_c, _ = await chat_with()
    await kb.set_chat_kb(conn, chosen.id, always_retrieve=True)
    await settings.put(conn, kb.LOOKUP, False)
    assert (await kb.get_chat_kb(conn, untouched.id)).always_retrieve is False
    assert not looked_up((await go(conn, untouched, user))[0])
    assert (await kb.get_chat_kb(conn, chosen.id)).always_retrieve is True      # his choice for one chat stands
    assert looked_up((await go(conn, chosen, user_c))[0])
    await settings.put(conn, kb.LOOKUP, True)
    assert (await kb.get_chat_kb(conn, untouched.id)).always_retrieve is True


async def test_a_chat_whose_switch_was_on_before_there_was_a_default_stays_on(conn, chat_with):
    """Before, a switch was off until somebody set it: a row that says "on" is a choice. One that says "off" may
    be nobody's, and follows what holds for every chat."""
    on, _, _ = await chat_with()
    off, _, _ = await chat_with()
    await conn.execute("update chat_kb set always_retrieve=1, retrieve_chosen=0 where chat_id=?", (on.id,))
    await conn.execute("update chat_kb set always_retrieve=0, retrieve_chosen=0 where chat_id=?", (off.id,))
    await conn.commit()
    await settings.put(conn, kb.LOOKUP, False)
    assert (await kb.get_chat_kb(conn, on.id)).always_retrieve is True
    assert (await kb.get_chat_kb(conn, off.id)).always_retrieve is False
    await settings.put(conn, kb.LOOKUP, True)
    assert (await kb.get_chat_kb(conn, off.id)).always_retrieve is True


async def test_a_database_from_before_gets_the_two_columns(conn):
    """An install that exists has the table without them."""
    await conn.executescript("drop table chat_kb; create table chat_kb (chat_id text primary key, always_retrieve integer not null default 0);")
    await db.migrate(conn)
    have = {r["name"] for r in await (await conn.execute("pragma table_info(chat_kb)")).fetchall()}
    assert {"emptied", "retrieve_chosen"} <= have


async def test_settings_carry_the_switch_for_every_chat(client, conn):
    assert (await client.get("/api/settings")).json()["kb_lookup"] is True
    assert (await client.put("/api/settings", json={"kb_lookup": False})).json()["kb_lookup"] is False
    assert await settings.get(conn, kb.LOOKUP) is False
    rag = await make_collection(conn, "RAG")
    chat = await new_chat(client)
    await client.put(f"/api/kb/chats/{chat['id']}", json={"collection_ids": [rag.id]})
    assert (await client.get(f"/api/kb/chats/{chat['id']}")).json()["always_retrieve"] is False
    assert (await client.put("/api/settings", json={"kb_lookup": True})).json()["kb_lookup"] is True
    assert (await client.get(f"/api/kb/chats/{chat['id']}")).json()["always_retrieve"] is True


async def test_a_chat_that_follows_the_named_collections_looks_them_up_and_is_offered_the_search(conn, tmp_path):
    """The two halves of what the owner asked for, together: a chat he never touched reads his collection, has it
    looked up for every message, and its agent has the tool to search it again."""
    from hunterai.tools import native
    from tests.toolkit import Turns
    native.register_all()
    p = Turns("abliteration"); use(abliteration=p)
    col = await make_collection(conn, "RAG")
    await ingest_texts(conn, col, tmp_path, {"notes.md": "waf bypass notes"})
    chat = await chats.create(conn, None, "pentesting", "abliteration", "abliterated-model", {})
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="waf bypass notes")])
    await kb.set_new_chat_collections(conn, [col.id])                            # named after the chat was made
    run, msg = await go(conn, chat, user)
    assert looked_up(run) and msg.blocks[-1].items[0].source_uri.endswith("notes.md")
    assert "kb_search" in [t.name for t in p.calls[0]["tools"]]


async def test_the_model_is_told_that_the_search_for_this_message_was_made(conn, chat_with):
    """Its rules say to search before answering from memory. With the passages already in the message, a model that
    follows them to the letter would make the same search again: one more call of a slow local model for nothing."""
    from hunterai.kb import retrieve
    from hunterai.tools import native
    from tests.toolkit import Turns
    native.register_all()
    p = Turns("abliteration"); use(abliteration=p)
    chat, user, _ = await chat_with()
    await go(conn, chat, user)
    said = p.calls[0]["system"]
    assert retrieve.LOOKED_UP in said and said.index(retrieve.LOOKED_UP) > said.index("KB_REFERENCE")
    for needle in ("already searched", "Do not repeat that search", "other words or an exact name"):
        assert needle in retrieve.LOOKED_UP
    await kb.set_chat_kb(conn, chat.id, always_retrieve=False)                 # nothing was looked up: nothing is claimed
    user2 = await messages.add(conn, chat.id, None, "user", [TextBlock(text="waf bypass notes")])
    await go(conn, chat, user2)
    assert retrieve.LOOKED_UP not in p.calls[1]["system"]


async def test_a_model_without_tools_is_not_told_to_search_again(conn, chat_with):
    from hunterai.kb import retrieve
    p = Scripted("abliteration"); use(abliteration=p)            # this provider takes no tools: there is no kb_search to name
    chat, user, _ = await chat_with()
    await go(conn, chat, user)
    assert "KB_REFERENCE" in p.calls[0]["system"] and retrieve.LOOKED_UP not in p.calls[0]["system"]


async def test_the_room_for_sources_counts_what_the_model_is_told_about_the_search(conn, chat_with):
    """In a small local window the sources get what is left: the sentence about the search takes its share."""
    from hunterai.kb import retrieve
    from hunterai.providers.base import WireMessage
    from hunterai.repo import runs
    chat, user, _ = await chat_with()
    chat = await chats.update(conn, chat.id, provider="ollama", options={"context_window": 4096})
    wire, budgets = [WireMessage("user", [TextBlock(text="waf bypass notes")])], []
    for can_search in (False, True):
        run = await runs.create(conn, chat.id, user.id, None)
        await retrieve.augment(conn, run.id, chat, "S" * 7000, wire, can_search=can_search)
        budgets.append((await runs.get(conn, run.id)).steps[0].detail["budget_chars"])
    assert 0 < budgets[1] < 3072 and budgets[0] - budgets[1] == len(retrieve.LOOKED_UP) + 1


async def test_the_lookup_and_the_recall_of_notes_embed_the_message_once(conn, chat_with, embed_spy):
    """Both search with the user's message. Each request to the embedder loads the model and unloads it behind its
    answer, on a machine whose graphics card the chat model already fills: one request per message, not two."""
    from hunterai import memory
    use(abliteration=Scripted("abliteration"))
    await settings.put(conn, "memory_embedder", "fake:64")
    await memory.add(conn, "the waf notes of acme are in the wiki.")
    chat, user, _ = await chat_with("waf bypass notes")
    embed_spy.batches.clear()
    run, msg = await go(conn, chat, user)
    assert [(s.kind, s.name, s.status) for s in run.steps][:2] == [("retrieval", "kb_search", "done"), ("retrieval", "memory", "done")]
    assert embed_spy.batches == [["waf bypass notes"]]
    assert run.steps[1].detail["used"] == 1 and msg.blocks[-1].items                 # and both found what they look for


async def test_two_embedders_are_two_requests(conn, chat_with, embed_spy):
    from hunterai import memory
    use(abliteration=Scripted("abliteration"))
    await settings.put(conn, "memory_embedder", "fake:32")                       # another model than the collection's
    await memory.add(conn, "the waf notes of acme are in the wiki.")
    chat, user, _ = await chat_with("waf bypass notes")
    embed_spy.batches.clear()
    await go(conn, chat, user)
    assert embed_spy.batches == [["waf bypass notes"], ["waf bypass notes"]]


async def test_a_search_embeds_a_query_it_was_not_given_the_vector_of(conn, chat_with, embed_spy):
    from hunterai.kb import search
    _, _, col = await chat_with()
    known = search.QueryVectors()
    embed_spy.batches.clear()
    first = await search.search(conn, [col.id], "waf bypass", vectors=known)
    again = await search.search(conn, [col.id], "waf bypass", vectors=known)
    other = await search.search(conn, [col.id], "bypass notes", vectors=known)
    assert embed_spy.batches == [["waf bypass"], ["bypass notes"]] and first == again and other
    await search.search(conn, [col.id], "waf bypass")                           # nobody's vectors: asked as before
    assert embed_spy.batches == [["waf bypass"], ["bypass notes"], ["waf bypass"]]


async def test_an_embedder_that_did_not_answer_is_not_waited_for_twice_in_one_run(conn, chat_with, embed_spy, monkeypatch):
    """The lookup waited its half minute for a busy embedder and went on with the keyword index. The recall of notes,
    made with the same words a moment later, is told the same answer instead of waiting for it once more."""
    from hunterai import memory
    from hunterai.kb import search
    from hunterai.kb.embed import EmbedError
    use(abliteration=Scripted("abliteration"))
    await settings.put(conn, "memory_embedder", "fake:64")
    await memory.add(conn, "the waf notes of acme are in the wiki.")
    chat, user, _ = await chat_with("waf bypass notes")
    asked, real = [], search.embed.get

    def get(spec, lock_timeout=None, **kw):
        asked.append(spec)
        return real(spec, lock_timeout, **kw)
    monkeypatch.setattr(search.embed, "get", get)
    embed_spy.fail = EmbedError("unavailable", "embedder busy")
    run, _ = await go(conn, chat, user)
    assert asked == ["fake:64"]                                                 # one request, one wait
    assert [(s.name, s.status) for s in run.steps][:2] == [("kb_search", "done"), ("memory", "error")]
    assert run.steps[0].detail["keywords_only"] == "embedder busy" and run.steps[1].detail["error"] == "embedder busy"
    assert run.status == "done"
