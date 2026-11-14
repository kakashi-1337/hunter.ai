import pytest

from hunterai.repo import chats, messages, search
from hunterai.types import TextBlock


@pytest.fixture
async def chat(conn):
    return await chats.create(conn, None, "pentesting", "mock", "m", {})


async def test_search_finds_text(conn, chat):
    await messages.add(conn, chat.id, None, "user", [TextBlock(text="hello world")])
    m = await messages.add(conn, chat.id, None, "user", [TextBlock(text="try this sqli payload")])
    hits = await search.query(conn, "sqli payload")
    assert [h.message_id for h in hits] == [m.id]
    assert hits[0].chat_id == chat.id and hits[0].chat_title == "New chat"
    assert "sqli" in hits[0].snippet


@pytest.mark.parametrize("q", ['"unterminated', 'a AND', 'foo*bar', '-x', 'col:val', '()', '',
                               '   ', 'NEAR(a b)', "it's", '^start', 'a"b"c'])
async def test_search_never_raises(conn, chat, q):
    await messages.add(conn, chat.id, None, "user", [TextBlock(text="a AND col:val it's")])
    assert isinstance(await search.query(conn, q), list)


async def test_special_characters_match_literally(conn, chat):
    m = await messages.add(conn, chat.id, None, "user", [TextBlock(text="header X-Api-Key: abc")])
    assert [h.message_id for h in await search.query(conn, "X-Api-Key:")] == [m.id]


async def test_deleted_message_not_searchable(conn, chat):
    u = await messages.add(conn, chat.id, None, "user", [TextBlock(text="needle one")])
    await messages.add(conn, chat.id, u.id, "assistant", [TextBlock(text="needle two")])
    await messages.delete(conn, u.id)
    assert await search.query(conn, "needle") == []


async def test_deleted_chat_not_searchable(conn, chat):
    await messages.add(conn, chat.id, None, "user", [TextBlock(text="needle")])
    await chats.delete(conn, chat.id)
    assert await search.query(conn, "needle") == []


async def test_updated_message_reindexed(conn, chat):
    m = await messages.add(conn, chat.id, None, "assistant", [TextBlock(text="alpha")])
    await messages.update(conn, m.id, blocks=[TextBlock(text="bravo")])
    assert await search.query(conn, "alpha") == []
    assert len(await search.query(conn, "bravo")) == 1
