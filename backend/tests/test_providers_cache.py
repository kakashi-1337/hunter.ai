import asyncio

import httpx
import pytest

from hunterai.api import providers as providers_api
from hunterai.api.app import create_app
from hunterai.providers import registry
from hunterai.providers.base import ModelInfo

BASE = "http://127.0.0.1:8787"


class Flaky:
    """Fails for the first `bad` listings (the way `mode` says), then lists one model."""

    def __init__(self, name, mode, bad=1, delay=0.0):
        self.name, self.mode, self.bad, self.delay, self.calls = name, mode, bad, delay, 0

    def configured(self):
        return True

    async def models(self):
        self.calls += 1
        if self.calls <= self.bad:
            if self.mode == "raise":
                raise RuntimeError("boom")
            if self.mode == "slow":
                await asyncio.sleep(self.delay)
            return []  # what the real Ollama adapter returns when it is down
        return [ModelInfo(id="m1", label="m1")]


@pytest.fixture
async def client(monkeypatch, isolated_home):
    monkeypatch.setenv("HUNTERAI_MOCK", "1")
    monkeypatch.setattr(providers_api, "MODEL_TIMEOUT", 0.05)
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
            yield c


async def listing(client):
    r = await client.get("/api/providers")
    assert r.status_code == 200
    return {p["name"]: p for p in r.json()}


@pytest.mark.parametrize("mode", ["empty", "raise", "slow"])
async def test_failed_listing_is_retried_on_the_next_request(client, monkeypatch, mode):
    monkeypatch.setattr(providers_api, "FAILED_SECONDS", 0, raising=False)
    flaky = Flaky("ollama", mode, delay=1)
    registry.override("ollama", flaky)
    assert (await listing(client))["ollama"]["models"] == []
    assert [m["id"] for m in (await listing(client))["ollama"]["models"]] == ["m1"]
    assert flaky.calls == 2
    await listing(client)
    assert flaky.calls == 2  # the successful listing is cached


async def test_one_failing_provider_does_not_uncache_the_good_ones(client, monkeypatch):
    monkeypatch.setattr(providers_api, "FAILED_SECONDS", 0, raising=False)
    down = Flaky("ollama", "empty", bad=10**6)
    good = Flaky("codex", "empty", bad=0)
    registry.override("ollama", down)
    registry.override("codex", good)
    for _ in range(3):
        ps = await listing(client)
    assert ps["ollama"]["models"] == [] and ps["codex"]["models"][0]["id"] == "m1"
    assert down.calls == 3 and good.calls == 1


async def test_failed_listing_is_not_hammered_within_the_short_window(client):
    assert 0 < providers_api.FAILED_SECONDS <= 10
    down = Flaky("ollama", "empty", bad=10**6)
    registry.override("ollama", down)
    await listing(client)
    await listing(client)
    assert down.calls == 1
