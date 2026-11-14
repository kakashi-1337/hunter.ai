import httpx
import pytest

from hunterai.api.app import create_app
from hunterai.repo import chats, messages, search
from hunterai.types import TextBlock

BASE = "http://127.0.0.1:8787"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>HunterAI</title>")
    monkeypatch.setenv("HUNTERAI_DIST", str(dist))
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            yield c


async def test_search_query_treats_nul_as_whitespace(conn):
    chat = await chats.create(conn, None, "pentesting", "mock", "m", {})
    m = await messages.add(conn, chat.id, None, "user", [TextBlock(text="alpha bravo")])
    assert [h.message_id for h in await search.query(conn, "alpha\x00bravo")] == [m.id]
    assert await search.query(conn, "\x00") == []


async def test_search_endpoint_with_nul_returns_200(client):
    r = await client.get("/api/search?q=a%00b")
    assert r.status_code == 200 and r.json() == []


async def test_static_path_with_nul_gets_what_other_unknown_paths_get(client):
    r = await client.get("/a%00b")
    other = await client.get("/some/route")
    assert (r.status_code, r.text) == (other.status_code, other.text) == (200, other.text)
    assert "HunterAI" in r.text


async def test_static_asset_path_with_nul_is_404(client):
    assert (await client.get("/assets/a%00b")).status_code == 404
