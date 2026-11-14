import pytest

from hunterai.repo import chats, messages, targets
from hunterai.types import FileBlock, ImageBlock, TextBlock, ThinkingBlock


def T(s):
    return [TextBlock(text=s)]


@pytest.fixture
async def chat(conn):
    return await chats.create(conn, None, "pentesting", "mock", "m", {})


async def ids(conn, chat):
    return [m.id for m in await messages.path(conn, chat.id)]


async def test_new_chat_defaults(conn, chat):
    assert chat.title == "New chat" and chat.active_leaf_id is None
    assert await messages.path(conn, chat.id) == []


async def test_path_follows_active_leaf(conn, chat):
    u1 = await messages.add(conn, chat.id, None, "user", T("a"))
    a1 = await messages.add(conn, chat.id, u1.id, "assistant", T("b"))
    assert await ids(conn, chat) == [u1.id, a1.id]
    assert (await chats.get(conn, chat.id)).active_leaf_id == a1.id


async def test_edit_creates_sibling_branch_and_keeps_old(conn, chat):
    u1 = await messages.add(conn, chat.id, None, "user", T("a"))
    a1 = await messages.add(conn, chat.id, u1.id, "assistant", T("b"))
    u1b = await messages.add(conn, chat.id, None, "user", T("a2"))
    assert await messages.siblings(conn, u1.id) == [u1.id, u1b.id]
    assert await ids(conn, chat) == [u1b.id]
    await messages.set_active(conn, chat.id, u1.id)
    assert await ids(conn, chat) == [u1.id, a1.id]


async def test_set_active_follows_newest_child(conn, chat):
    u1 = await messages.add(conn, chat.id, None, "user", T("a"))
    await messages.add(conn, chat.id, u1.id, "assistant", T("old"))
    a2 = await messages.add(conn, chat.id, u1.id, "assistant", T("new"))
    u2 = await messages.add(conn, chat.id, a2.id, "user", T("next"))
    await messages.set_active(conn, chat.id, u1.id)
    assert await ids(conn, chat) == [u1.id, a2.id, u2.id]


async def test_delete_subtree_moves_leaf_to_parent(conn, chat):
    u1 = await messages.add(conn, chat.id, None, "user", T("a"))
    a1 = await messages.add(conn, chat.id, u1.id, "assistant", T("b"))
    u2 = await messages.add(conn, chat.id, a1.id, "user", T("c"))
    await messages.delete(conn, a1.id)
    assert await ids(conn, chat) == [u1.id]
    assert await messages.get(conn, u2.id) is None


async def test_delete_root_clears_leaf(conn, chat):
    u1 = await messages.add(conn, chat.id, None, "user", T("a"))
    await messages.delete(conn, u1.id)
    assert await ids(conn, chat) == []


async def test_delete_off_branch_keeps_leaf(conn, chat):
    u1 = await messages.add(conn, chat.id, None, "user", T("a"))
    u1b = await messages.add(conn, chat.id, None, "user", T("a2"))
    await messages.delete(conn, u1.id)
    assert await ids(conn, chat) == [u1b.id]


async def test_blocks_roundtrip_all_types(conn, chat):
    blocks = [ThinkingBlock(text="t"), TextBlock(text="x"),
              ImageBlock(upload_id="u", mime="image/png"),
              FileBlock(upload_id="f", filename="a.txt", text="hi", truncated=False)]
    m = await messages.add(conn, chat.id, None, "user", blocks)
    assert (await messages.get(conn, m.id)).blocks == blocks


async def test_update_changes_status_and_blocks(conn, chat):
    m = await messages.add(conn, chat.id, None, "assistant", [], status="streaming")
    m2 = await messages.update(conn, m.id, blocks=T("done"), status="complete",
                               provider="ollama", model="q", fallback_from="abliteration/x")
    assert (m2.status, m2.blocks[0].text, m2.provider, m2.fallback_from) == \
        ("complete", "done", "ollama", "abliteration/x")


async def test_delete_target_keeps_chats(conn):
    t = await targets.create(conn, "acme", "n", {"in": ["*.acme.test"], "out": []})
    c = await chats.create(conn, t.id, "plan", "mock", "m", {})
    await targets.delete(conn, t.id)
    assert (await chats.get(conn, c.id)).target_id is None


async def test_target_update_and_list(conn):
    t = await targets.create(conn, "acme")
    assert t.scope == {"in": [], "out": []}
    t2 = await targets.update(conn, t.id, name="acme2", scope={"in": ["a"], "out": ["b"]})
    assert [(x.name, x.scope) for x in await targets.list(conn)] == \
        [("acme2", {"in": ["a"], "out": ["b"]})]
    assert t2.id == t.id


async def test_chats_list_newest_first_and_filter(conn):
    t = await targets.create(conn, "acme")
    c1 = await chats.create(conn, t.id, "plan", "mock", "m", {})
    c2 = await chats.create(conn, None, "plan", "mock", "m", {})
    await messages.add(conn, c1.id, None, "user", T("bump"))
    assert [c.id for c in await chats.list(conn)] == [c1.id, c2.id]
    assert [c.id for c in await chats.list(conn, target_id=t.id)] == [c1.id]


async def test_chat_update_rejects_unknown_field(conn, chat):
    with pytest.raises(ValueError):
        await chats.update(conn, chat.id, id="x")
    c = await chats.update(conn, chat.id, title="t", options={"temperature": 0.2})
    assert c.title == "t" and c.options == {"temperature": 0.2}
