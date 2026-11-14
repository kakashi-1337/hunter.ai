import httpx
import pytest

from hunterai import config
from hunterai.api.app import create_app

BASE = "http://127.0.0.1:8790"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>HunterAI</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    secret = tmp_path / ".env"
    secret.write_text("ABLIT_API_KEY=ak_supersecret\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(secret))
    monkeypatch.setenv("HUNTERAI_DIST", str(dist))
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            yield c


async def test_index_served_at_root_and_for_app_routes(client):
    for path in ("/", "/some/route", "/chats/abc"):
        r = await client.get(path)
        assert r.status_code == 200 and "HunterAI" in r.text
        assert r.headers["content-type"].startswith("text/html")


async def test_assets_served(client):
    r = await client.get("/assets/app.js")
    assert r.status_code == 200 and r.text == "console.log(1)"
    assert "javascript" in r.headers["content-type"]
    assert (await client.get("/assets/missing.js")).status_code == 404


async def test_unknown_api_path_is_json_404_not_the_app(client):
    r = await client.get("/api/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("path", ["/../.env", "/%2e%2e/.env", "/assets/../../.env",
                                  "/assets/%2e%2e/%2e%2e/.env", "/assets/..%2f..%2f.env",
                                  "/.env", "//etc/passwd"])
async def test_no_path_escapes_the_dist_folder(client, path):
    r = await client.get(path)
    assert "ak_supersecret" not in r.text and "root:" not in r.text


async def test_without_a_build_the_root_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTERAI_DIST", str(tmp_path / "nope"))
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            r = await c.get("/")
            assert r.status_code == 404 and "scripts/start.sh" in r.text
            assert (await c.get("/api/settings")).status_code == 200


def test_dist_dir_default_points_at_frontend_build():
    assert config.dist_dir() == config.PROJECT_ROOT / "frontend" / "dist"
