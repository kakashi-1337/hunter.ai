import json
import logging

import httpx
import pytest

from hunterai import config
from hunterai.api.app import create_app
from hunterai.providers import registry
from hunterai.providers.base import ModelInfo

BASE = "http://127.0.0.1:8787"
SECRET = "ak_secret123"


class Slow:
    name = "ollama"

    def __init__(self, delay=0.0, fail=False, name="ollama"):
        self.delay, self.fail, self.calls, self.name = delay, fail, 0, name

    def configured(self):
        return True

    async def models(self):
        import asyncio
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return [ModelInfo(id="qwen3:8b", label="qwen3:8b")]


@pytest.fixture
async def client(monkeypatch, isolated_home):
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    registry.override("ollama", Slow())
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            yield c


async def test_settings_never_return_key_values(client, caplog):
    caplog.set_level(logging.DEBUG)
    r = await client.put("/api/settings/keys/ABLIT_API_KEY", json={"value": SECRET})
    assert r.status_code == 204 and r.content == b""
    assert config.env("ABLIT_API_KEY") == SECRET
    bodies = [(await client.get("/api/settings")).text, (await client.get("/api/providers")).text]
    assert all(SECRET not in b for b in bodies)
    assert SECRET not in caplog.text
    assert json.loads(bodies[0])["keys"]["ABLIT_API_KEY"] is True


async def test_settings_view_and_update(client):
    s = (await client.get("/api/settings")).json()
    assert s["default_provider"] == "abliteration" and s["fallback_model"] == "qwen3:8b"
    assert s["default_model"] == "abliterated-model-large-v2"
    assert set(s["keys"]) == {"ABLIT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                              "XAI_API_KEY", "GEMINI_API_KEY"}
    assert not any(s["keys"].values())
    s = (await client.put("/api/settings", json={"fallback_model": "llama3.1:latest"})).json()
    assert s["fallback_model"] == "llama3.1:latest"


@pytest.mark.parametrize("name", ["OTHERTOOL_LLM", "OTHERSVC_API_KEY", "NOPE"])
async def test_key_endpoint_rejects_other_names(client, name):
    before = config.env_file().read_text()
    r = await client.put(f"/api/settings/keys/{name}", json={"value": "x"})
    assert r.status_code == 400 and config.env_file().read_text() == before


async def test_key_endpoint_rejects_bad_values(client):
    assert (await client.put("/api/settings/keys/ABLIT_API_KEY",
                             json={"value": "a\nINJECT=1"})).status_code == 400
    assert (await client.put("/api/settings/keys/ABLIT_API_KEY",
                             json={"value": "   "})).status_code == 400


async def test_the_webhook_address_is_saved_from_settings(client):
    hook = "https://discord.com/api/webhooks/123456789012345678/abcDEF_123-xyz"
    assert (await client.get("/api/settings")).json()["notify_webhook_set"] is False
    r = await client.put("/api/settings/keys/DISCORD_WEBHOOK_URL", json={"value": f"  {hook} "})
    assert r.status_code == 204, r.text
    assert config.env("DISCORD_WEBHOOK_URL") == hook
    assert (await client.get("/api/settings")).json()["notify_webhook_set"] is True
    bad = await client.put("/api/settings/keys/DISCORD_WEBHOOK_URL", json={"value": "https://evil.test/api/webhooks/1/a"})
    assert bad.status_code == 400 and config.env("DISCORD_WEBHOOK_URL") == hook          # a refused value changes nothing


def test_every_name_settings_offers_to_save_can_be_written():
    from hunterai import envfile
    from hunterai.api import settings as settings_api
    assert set(settings_api.SECRET_NAMES) <= envfile.ALLOWED


async def test_providers_lists_configured_flag_and_models(client):
    ps = {p["name"]: p for p in (await client.get("/api/providers")).json()}
    assert list(ps)[0] == "abliteration" and "mock" in ps
    assert ps["abliteration"]["configured"] is False and ps["abliteration"]["models"] == []
    assert ps["mock"]["configured"] is True and ps["mock"]["models"][0]["id"] == "mock-1"
    assert ps["ollama"]["models"][0]["id"] == "qwen3:8b"
    assert set(ps["mock"]["models"][0]) >= {"id", "label", "supports_images", "options",
                                            "context_window"}


async def test_providers_cached_until_a_key_changes(client):
    slow = registry.get("ollama")
    await client.get("/api/providers")
    await client.get("/api/providers")
    assert slow.calls == 1
    await client.put("/api/settings/keys/XAI_API_KEY", json={"value": "k"})
    await client.get("/api/providers")
    assert slow.calls == 2


async def test_slow_or_broken_provider_does_not_break_the_list(client, monkeypatch):
    from hunterai.api import providers as providers_api
    monkeypatch.setattr(providers_api, "MODEL_TIMEOUT", 0.05)
    providers_api.clear_cache()
    registry.override("ollama", Slow(delay=1))
    registry.override("codex", Slow(fail=True, name="codex"))
    r = await client.get("/api/providers")
    assert r.status_code == 200
    ps = {p["name"]: p for p in r.json()}
    assert ps["ollama"]["models"] == [] and ps["mock"]["models"]


async def test_kb_redact_is_on_by_default_and_can_be_switched(client):
    assert (await client.get("/api/settings")).json()["kb_redact"] is True
    assert (await client.put("/api/settings", json={"kb_redact": False})).json()["kb_redact"] is False
    assert (await client.put("/api/settings", json={"max_turns": 10})).json()["kb_redact"] is False


async def test_the_swarm_width_is_three_until_the_owner_sets_one_to_six(client):
    seen = (await client.get("/api/settings")).json()
    assert seen["swarm_width"] == 3 and seen["machine"]["cores"] >= 1
    assert (await client.put("/api/settings", json={"swarm_width": 5})).json()["swarm_width"] == 5
    for bad in (0, 7, -1):
        assert (await client.put("/api/settings", json={"swarm_width": bad})).status_code == 400
    assert (await client.get("/api/settings")).json()["swarm_width"] == 5
