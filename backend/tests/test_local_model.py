"""Where a local model runs and what its window costs are said on the page: the machine line names the share on
the processor, and the window setting says what a token of window takes for the model that is in use."""
import httpx
import pytest
import respx

from hunterai.api import system
from hunterai.providers import ollama
from tests.test_api import client  # noqa: F401 (client is a fixture)

HOST = "http://127.0.0.1:11434"
# What Ollama 0.40.16 shows for qwen3:0.6b, and what it was measured to take: 116,736 to 121,859 bytes for each token
# of window (the size /api/ps reports at 2,048, 8,192 and 16,384 tokens).
QWEN = {"general.architecture": "qwen3", "qwen3.attention.head_count": 16, "qwen3.attention.head_count_kv": 8,
        "qwen3.attention.key_length": 128, "qwen3.attention.value_length": 128, "qwen3.block_count": 28,
        "qwen3.context_length": 40960, "qwen3.embedding_length": 1024}
GB = 2**30


@pytest.mark.parametrize("size,vram,words", [(1000, 0, "100% CPU"), (1000, 1000, "100% GPU"), (1000, 550, "45%/55% CPU/GPU"),
                                              (1000, 1200, "unknown"), (0, 5, "unknown")])
def test_where_a_loaded_model_runs_is_said_in_the_words_of_ollama_ps(size, vram, words):
    assert ollama.processor({"size": size, "size_vram": vram}) == words


def test_what_a_token_of_window_takes_is_worked_out_from_the_layers_of_the_model():
    assert ollama.bytes_per_token(QWEN) == 114_688                      # measured: 116,736 to 121,859
    assert 0.90 < ollama.bytes_per_token(QWEN) / 121_859 < 1.0


def test_a_model_that_does_not_name_its_key_length_is_worked_out_from_its_width():
    info = {k: v for k, v in QWEN.items() if "key_length" not in k and "value_length" not in k and "head_count_kv" not in k}
    assert ollama.bytes_per_token(info) == 28 * 16 * (64 + 64) * 2      # width 1024 over 16 heads


@pytest.mark.parametrize("info", [{}, {"general.architecture": "qwen3"}, {"general.architecture": "qwen3", "qwen3.block_count": "many"},
                                  {"general.architecture": "qwen3", "qwen3.block_count": 28, "qwen3.attention.head_count": 0}])
def test_a_model_that_does_not_say_enough_has_no_figure(info):
    assert ollama.bytes_per_token(info) is None


def ollama_with(loaded=None, installed=("qwen3:8b",), info=QWEN):
    respx.get(HOST + "/api/ps").mock(return_value=httpx.Response(200, json={"models": loaded or []}))
    respx.get(HOST + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": n} for n in installed]}))
    respx.post(HOST + "/api/show").mock(return_value=httpx.Response(200, json={"capabilities": ["completion", "tools"], "model_info": info}))
    for base in ("http://127.0.0.1:6333", "http://127.0.0.1:8888"):
        respx.route(url__startswith=base).mock(side_effect=httpx.ConnectError("refused"))


@respx.mock
async def test_the_status_says_where_each_loaded_model_runs_and_with_which_window(client):
    ollama_with(loaded=[{"name": "gemma3:12b", "size": 10 * GB, "size_vram": 6 * GB, "context_length": 32768},
                        {"name": "qwen3:8b", "size": 5 * GB, "size_vram": 5 * GB, "context_length": 16384}])
    got = (await client.get("/api/system/status")).json()["local_models"]
    assert got == [{"name": "gemma3:12b", "vram_mb": 6144, "size_mb": 10240, "processor": "40%/60% CPU/GPU", "context": 32768},
                   {"name": "qwen3:8b", "vram_mb": 5120, "size_mb": 5120, "processor": "100% GPU", "context": 16384}]


@respx.mock
async def test_an_older_ollama_that_names_no_window_leaves_it_out(client):
    ollama_with(loaded=[{"name": "qwen3:8b", "size": 5 * GB, "size_vram": 0}])
    assert (await client.get("/api/system/status")).json()["local_models"] == [
        {"name": "qwen3:8b", "vram_mb": 0, "size_mb": 5120, "processor": "100% CPU", "context": None}]


@respx.mock
async def test_the_local_model_in_use_is_described_with_what_its_window_costs(client):
    ollama_with(loaded=[{"name": "qwen3:8b", "size": 6 * GB, "size_vram": 6 * GB, "context_length": 16384}])
    got = (await client.get("/api/system/local")).json()
    assert got["answers"] is True and (got["model"], got["installed"]) == ("qwen3:8b", True)
    assert (got["window"], got["limit"], got["bytes_per_token"]) == (16384, 40960, 114_688)
    assert got["loaded"] == {"processor": "100% GPU", "context": 16384, "size_mb": 6144}
    assert got["overhead"]["tools"] > 20 and 4000 < got["overhead"]["tokens"] < 12000 and got["overhead"]["on_demand"] == 0
    assert got["enough"] == 16384                                       # the rules and tools, 8,192 for the chat, to the next 4,096


@respx.mock
async def test_a_model_ollama_lists_with_the_tag_it_was_not_given_is_the_same_model(client):
    # Ollama writes "latest" where a model was pulled or called without a tag.
    await client.put("/api/settings", json={"fallback_model": "mimo"})
    ollama_with(installed=("mimo:latest",), loaded=[{"name": "mimo:latest", "size": 6 * GB, "size_vram": 6 * GB, "context_length": 16384}])
    got = (await client.get("/api/system/local")).json()
    assert (got["model"], got["installed"]) == ("mimo", True) and got["loaded"]["context"] == 16384


@respx.mock
async def test_a_window_too_small_for_the_rules_and_tools_names_the_size_that_is_enough(client, monkeypatch):
    ollama_with()

    async def many(conn):
        return {"tokens": 25608, "tools": 112, "on_demand": 40, "window": 16384}
    monkeypatch.setattr(system, "overhead", many)
    got = (await client.get("/api/system/local")).json()
    assert (got["window"], got["enough"], got["loaded"]) == (16384, 36864, None)


@respx.mock
async def test_the_window_of_the_local_model_is_never_said_larger_than_the_model_takes(client):
    ollama_with(info={**QWEN, "qwen3.context_length": 8192})
    got = (await client.get("/api/system/local")).json()
    assert (got["window"], got["limit"]) == (8192, 8192)


@respx.mock
async def test_a_fallback_model_that_is_not_installed_is_said(client):
    ollama_with(installed=("gemma3:12b",))
    shown = respx.post(HOST + "/api/show").mock(return_value=httpx.Response(404, json={"error": "model 'qwen3:8b' not found"}))
    got = (await client.get("/api/system/local")).json()
    assert (got["answers"], got["installed"], got["limit"], got["bytes_per_token"], got["window"]) == (True, False, None, None, 16384)
    assert not shown.called                                             # Ollama is not asked about a model it does not have


@respx.mock
async def test_of_the_loaded_models_the_one_in_use_is_the_one_described(client):
    ollama_with(loaded=[{"name": "gemma3:12b", "size": 10 * GB, "size_vram": 6 * GB, "context_length": 32768},
                        {"name": "qwen3:8b", "size": 5 * GB, "size_vram": 5 * GB, "context_length": 8192}])
    assert (await client.get("/api/system/local")).json()["loaded"] == {"processor": "100% GPU", "context": 8192, "size_mb": 5120}


@respx.mock
async def test_without_ollama_the_local_model_is_described_as_far_as_the_settings_go(client):
    for base in (HOST, "http://127.0.0.1:6333", "http://127.0.0.1:8888"):
        respx.route(url__startswith=base).mock(side_effect=httpx.ConnectError("refused"))
    got = (await client.get("/api/system/local")).json()
    assert (got["answers"], got["installed"], got["limit"], got["bytes_per_token"], got["loaded"]) == (False, False, None, None, None)
    assert got["model"] == "qwen3:8b" and got["window"] == 16384 and got["overhead"]["tokens"] > 0


@respx.mock
async def test_the_diagnostics_name_the_local_model_its_window_and_what_runs_now(client):
    ollama_with(loaded=[{"name": "qwen3:8b", "size": 6 * GB, "size_vram": 3 * GB, "context_length": 16384}])
    text = (await client.get("/api/system/diagnostics")).json()["text"]
    assert "\nLocal models loaded: qwen3:8b (6.0 GB, 50%/50% CPU/GPU, window 16,384)\n" in text
    assert "\nLocal model in use: qwen3:8b, window 16,384 of its own 40,960; about 0.9 GB for each 8,192 tokens of window\n" in text
    assert "\nA new chat sends about " in text and " tokens of rules and tools (" in text


# ---- what Ollama holds in memory

PS = HOST + "/api/ps"


@respx.mock
@pytest.mark.parametrize("held, model, want", [
    (["qwen3:8b"], "qwen3:8b", True),
    (["bge-m3:latest", "qwen3:8b"], "qwen3:8b", True),
    (["qwen3:latest"], "qwen3", True),                                    # Ollama writes the tag it was not given
    (["qwen3"], "qwen3:latest", True),
    (["bge-m3:latest"], "qwen3:8b", False),
    ([], "qwen3:8b", False),
    (["qwen3:8b-q4_K_M"], "qwen3:8b", False),                             # another model whose name begins alike
    (["qwen3:8b"], "qwen3", False),                                       # "qwen3" is qwen3:latest, not any qwen3
])
async def test_ollama_is_asked_whether_a_model_is_in_memory(held, model, want):
    respx.get(PS).mock(return_value=httpx.Response(200, json={"models": [{"name": n, "model": n, "size": 1} for n in held]}))
    assert await ollama.in_memory(model) is want


@respx.mock
async def test_a_model_is_found_under_either_of_the_names_ollama_gives_it():
    respx.get(PS).mock(return_value=httpx.Response(200, json={"models": [{"model": "qwen3:8b"}, {"name": "eyes:9b"}, "odd", None]}))
    assert await ollama.in_memory("qwen3:8b") is True and await ollama.in_memory("eyes:9b") is True
    assert await ollama.in_memory("odd") is False


@respx.mock
@pytest.mark.parametrize("answer", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("no answer in time"),
    httpx.Response(500, text="oops"), httpx.Response(404, json={"error": "an Ollama too old to have the route"}),
    httpx.Response(200, text="not json"), httpx.Response(200, json=["a", "list"]), httpx.Response(200, json={"models": "none"}),
    httpx.Response(200, json={}),
])
async def test_an_ollama_that_does_not_say_what_it_holds_is_not_guessed_at(answer):
    respx.get(PS).mock(side_effect=answer) if isinstance(answer, Exception) else respx.get(PS).mock(return_value=answer)
    assert await ollama.in_memory("qwen3:8b") is None


@respx.mock
async def test_the_question_goes_straight_to_ollama_and_does_not_wait_long(monkeypatch):
    # A proxy named in the environment is for the providers out there, not for a program on this machine. And the
    # question stands before a call: an Ollama that is slow to answer it is taken as one that does not say.
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    seen = []
    real = httpx.AsyncClient
    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: (seen.append(kw), real(**kw))[1])
    respx.get(PS).mock(return_value=httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]}))
    assert await ollama.in_memory("qwen3:8b") is True
    assert [kw["trust_env"] for kw in seen] == [False]
    assert seen[0]["timeout"].read <= 5 and seen[0]["timeout"].connect <= 5


@respx.mock
async def test_the_ollama_that_is_asked_is_the_one_the_calls_go_to(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "localhost:11500")
    asked = respx.get("http://localhost:11500/api/ps").mock(return_value=httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]}))
    assert await ollama.in_memory("qwen3:8b") is True and asked.called
