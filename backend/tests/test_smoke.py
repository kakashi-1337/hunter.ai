"""scripts/smoke.sh: checks against the real services of an install. Here the services are stand-ins; the
checks, their words and their verdicts are the real ones."""
import json
import time

import httpx
import pytest
import respx

from hunterai import config, context, hop, smoke, upstream
from hunterai.providers import registry
from hunterai.providers.base import ProviderError, Stop, TextDelta, Usage
from hunterai.repo import settings
from tests.test_api import client  # noqa: F401 (client is a fixture)

OLLAMA = "http://127.0.0.1:11434"
SERVER = "http://127.0.0.1:8790"
QDRANT = "http://127.0.0.1:6333"
SEARX = "http://127.0.0.1:8888"
MODEL = "qwen3:8b"


async def run(conn, *only, deep=False, calls=False) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = await smoke.run(conn, only=list(only) or None, deep=deep, calls=calls, out=lines.append)
    return code, lines


def said(lines: list[str], name: str) -> list[tuple[str, str]]:
    """(verdict, words) of each line of one check."""
    out = []
    for line in lines:
        verdict, _, rest = line.partition(" ")
        label, _, words = rest.strip().partition("  ")
        if label.strip() == name:
            out.append((verdict, words.strip()))
    return out


def line(obj) -> str:
    return json.dumps(obj) + "\n"


def nothing_answers():
    for base in (OLLAMA, SERVER, QDRANT, SEARX):
        respx.route(url__startswith=base).mock(side_effect=httpx.ConnectError("refused"))


def ollama_with(*models, version="0.40.16"):
    respx.get(OLLAMA + "/api/version").mock(return_value=httpx.Response(200, json={"version": version}))
    respx.get(OLLAMA + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": m} for m in models]}))


def local_model(answer="PAPAYA-7731", *, context_length=40960, loaded=16384, vram=1.0, status=200):
    """The stand-in Ollama's side of a chat with MODEL: what it shows, what it answers, where it runs it."""
    respx.post(OLLAMA + "/api/show").mock(return_value=httpx.Response(200, json={
        "capabilities": ["completion", "tools"],
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": context_length}}))
    body = (line({"message": {"role": "assistant", "content": answer}, "done": False})
            + line({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                    "prompt_eval_count": 84, "eval_count": 10}))
    chat = respx.post(OLLAMA + "/api/chat").mock(return_value=httpx.Response(status, text=body if status == 200 else '{"error":"out of memory"}'))
    size = 5 * 2**30
    respx.get(OLLAMA + "/api/ps").mock(return_value=httpx.Response(200, json={"models": [
        {"name": MODEL, "size": size, "size_vram": int(size * vram), "context_length": loaded}]}))
    return chat


async def in_use(conn, name: str) -> None:
    await settings.put(conn, f"answered:{name}", time.time() - 3600)


# ---- the server

@respx.mock
async def test_a_server_that_is_not_running_is_skipped_and_stops_nothing(conn):
    nothing_answers()
    code, lines = await run(conn, "server")
    assert said(lines, "HunterAI") == [("skip", f"not running at {SERVER} (the other checks do not need it)")]
    assert code == 0


@respx.mock
async def test_a_server_on_the_code_that_is_on_disk_is_in_order(conn):
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={"restart_needed": False, "stopped": []}))
    assert said((await run(conn, "server"))[1], "HunterAI") == [("ok", f"runs at {SERVER} on the code that is on disk")]


@respx.mock
async def test_a_server_on_old_code_fails_and_says_to_restart(conn):
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={"restart_needed": True, "stopped": []}))
    code, lines = await run(conn, "server")
    assert said(lines, "HunterAI") == [("FAIL", f"runs at {SERVER} on code from before the last change: press Ctrl+C where it "
                                                "runs, then start it again (scripts/start.sh)")]
    assert code == 1


@respx.mock
async def test_a_server_from_before_it_could_tell_is_old_code_too(conn):
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={"cpu_temp": None}))
    assert said((await run(conn, "server"))[1], "HunterAI")[0][0] == "FAIL"


@respx.mock
async def test_what_the_server_says_is_not_working_is_passed_on(conn):
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={
        "restart_needed": False, "stopped": [{"id": "searxng", "label": "SearXNG (web_search)"}, {"id": "mcp", "label": "MCP server rea"}]}))
    assert said((await run(conn, "server"))[1], "HunterAI")[1] == ("note", "it says these are not working: SearXNG (web_search), MCP server rea")


# ---- Ollama

@respx.mock
async def test_ollama_that_answers_names_its_version_and_models(conn):
    ollama_with("qwen3:8b", "bge-m3:latest", "gemma3:12b")
    assert said((await run(conn, "ollama"))[1], "Ollama") == [("ok", f"0.40.16 at {OLLAMA}, 3 models")]


@respx.mock
async def test_ollama_that_was_never_in_use_here_is_skipped(conn):
    nothing_answers()
    code, lines = await run(conn, "ollama")
    assert said(lines, "Ollama") == [("skip", f"no answer at {OLLAMA} (it was not in use here)")]
    assert code == 0


@respx.mock
async def test_ollama_that_was_in_use_and_does_not_answer_fails(conn):
    nothing_answers()
    await in_use(conn, "ollama")
    code, lines = await run(conn, "ollama")
    assert said(lines, "Ollama") == [("FAIL", f"no answer at {OLLAMA}: start it with `ollama serve`")]
    assert code == 1


# ---- the local model

@respx.mock
async def test_a_local_model_that_follows_the_conversation_is_in_order(conn):
    """What was reported: the local model that answers in place of the chat's model did not know the conversation."""
    ollama_with(MODEL, "bge-m3:latest")
    chat = local_model(vram=0.55)
    got = said((await run(conn, "local"))[1], "local model")
    assert got[0][0] == "ok"
    assert got[0][1].startswith(f"{MODEL} repeated a word from two messages before, in ")
    assert got[0][1].endswith("s (84 tokens in, 10 out)")
    assert got[1] == ("ok", "it gets a window of 16,384 tokens of its own 40,960; Ollama runs it 45%/55% CPU/GPU with a window of 16,384")
    sent = json.loads(chat.calls[0].request.content)
    assert [m["role"] for m in sent["messages"]] == ["system", "user", "assistant", "user"]
    assert (sent["truncate"], sent["shift"], sent["options"]["num_ctx"]) == (False, False, 16384)        # as a chat sends it


@respx.mock
async def test_a_local_model_that_does_not_know_the_conversation_fails(conn):
    ollama_with(MODEL)
    local_model(answer="I do not know which word you mean. What would you like me to do?")
    code, lines = await run(conn, "local")
    verdict, words = said(lines, "local model")[0]
    assert verdict == "FAIL" and code == 1
    assert words == (f'{MODEL} did not repeat a word from two messages before. It answered: "I do not know which word you mean. '
                     'What would you like me to do?" Either the conversation does not reach it, or the model is too small to follow one.')


@respx.mock
async def test_a_local_model_ollama_fails_on_says_what_to_do(conn):
    ollama_with(MODEL)
    local_model(status=500)
    verdict, words = said((await run(conn, "local"))[1], "local model")[0]
    assert verdict == "FAIL" and "out of memory" in words and "`ollama ps`" in words


@respx.mock
async def test_a_window_loaded_with_another_size_is_said(conn):
    """Another program loaded the model with its own window: Ollama reloads it for HunterAI's call, which takes time."""
    ollama_with(MODEL)
    local_model(loaded=4096, vram=1.0)
    assert said((await run(conn, "local"))[1], "local model")[1][1].endswith("Ollama runs it 100% GPU with a window of 4,096")


@respx.mock
async def test_the_fallback_model_that_is_not_installed_fails_with_the_command(conn):
    ollama_with("gemma3:12b", "bge-m3:latest")
    code, lines = await run(conn, "local")
    assert said(lines, "local model") == [("FAIL", f"{MODEL}, the fallback model, is not installed: run `ollama pull {MODEL}`, or "
                                                   "choose an installed model as the fallback under Settings (installed: gemma3:12b)")]
    assert code == 1


@respx.mock
async def test_the_fallback_model_the_owner_chose_is_the_one_that_is_tried(conn):
    await settings.put(conn, "fallback_model", "gemma3:12b")
    ollama_with("gemma3:12b")
    local_model()
    assert said((await run(conn, "local"))[1], "local model")[0][1].startswith("gemma3:12b repeated a word")


@respx.mock
async def test_ollama_with_no_chat_model_is_a_note_not_a_failure(conn):
    ollama_with("bge-m3:latest")
    code, lines = await run(conn, "local")
    assert said(lines, "local model") == [("note", "Ollama has no chat model: the fallback cannot answer until one is pulled "
                                                   f"(`ollama pull {MODEL}`)")]
    assert code == 0


@respx.mock
async def test_without_ollama_the_local_model_is_skipped(conn):
    nothing_answers()
    assert said((await run(conn, "local"))[1], "local model") == [("skip", "Ollama does not answer")]


# ---- what a new chat sends to a local model before the conversation

@respx.mock
async def test_the_rules_and_tools_of_a_new_chat_are_measured_against_the_local_window(conn):
    ollama_with(MODEL)
    respx.route(url__startswith=SERVER).mock(side_effect=httpx.ConnectError("refused"))
    verdict, words = said((await run(conn, "window"))[1], "local window")[0]
    assert verdict == "ok"
    assert words.startswith("a new chat sends about ") and " tokens of rules and tools (" in words
    assert words.endswith("tools, without those of MCP servers: start HunterAI to count them); the window for local models is 16,384")


@respx.mock
async def test_rules_and_tools_that_fill_the_window_fail_with_the_size_to_set(conn, monkeypatch):
    """25,608 tokens for a window of 16,384 that stays as it is: the first request of every chat is refused."""
    monkeypatch.setenv("HUNTERAI_OLLAMA_NUM_CTX", "16384")
    monkeypatch.setenv("HUNTERAI_OLLAMA_MAX_CTX", "0")
    ollama_with(MODEL)
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={"restart_needed": False, "stopped": []}))
    respx.get(SERVER + "/api/system/overhead").mock(return_value=httpx.Response(200, json={"tokens": 25608, "tools": 112, "on_demand": 40, "window": 16384}))
    code, lines = await run(conn, "window")
    assert said(lines, "local window") == [("FAIL", (
        "a new chat sends about 25,608 tokens of rules and tools (112 tools, and 40 more that are loaded on demand); the window "
        'for local models is 16,384. Raise "Context window for local models" in Settings to 36,864 or more (a larger window '
        "needs more memory), or switch off the MCP servers you do not need"))]
    assert code == 1


@respx.mock
async def test_a_window_that_grows_by_itself_makes_small_room_a_thing_to_know_and_no_failure(conn, monkeypatch):
    """The same 25,608 tokens for a window of 16,384: the chat's window is raised before its first request goes out.
    It works, and every new chat begins with one more load of the model."""
    ollama_with(MODEL)
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={"restart_needed": False, "stopped": []}))
    respx.get(SERVER + "/api/system/overhead").mock(return_value=httpx.Response(200, json={"tokens": 25608, "tools": 112, "on_demand": 40, "window": 16384}))
    code, lines = await run(conn, "window")
    assert said(lines, "local window") == [("note", (
        "a new chat sends about 25,608 tokens of rules and tools (112 tools, and 40 more that are loaded on demand); the window "
        "for local models is 16,384. A chat's window grows by itself when it needs more room, up to 131,072 tokens, and the "
        'model is loaded again for each size. Raise "Context window for local models" in Settings to 36,864 or more to spare '
        "new chats that (a larger window needs more memory), or switch off the MCP servers you do not need"))]
    assert code == 0
    monkeypatch.setenv("HUNTERAI_OLLAMA_MAX_CTX", "32768")           # it may not grow as far as that takes
    code, lines = await run(conn, "window")
    assert said(lines, "local window")[0][0] == "FAIL" and code == 1


@respx.mock
async def test_the_running_server_counts_the_tools_of_mcp_servers_too(conn):
    ollama_with(MODEL)
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={"restart_needed": False, "stopped": []}))
    respx.get(SERVER + "/api/system/overhead").mock(return_value=httpx.Response(200, json={"tokens": 9100, "tools": 51, "on_demand": 96, "window": 32768}))
    assert said((await run(conn, "window"))[1], "local window") == [("ok", (
        "a new chat sends about 9,100 tokens of rules and tools (51 tools, and 96 more that are loaded on demand); the window "
        "for local models is 32,768"))]


@respx.mock
async def test_rules_and_tools_that_fit_but_leave_no_room_for_the_chat_fail_too(conn, monkeypatch):
    """12,000 of 16,384, in a window that stays as it is: the request is accepted, and the conversation has to be
    summarized from its second turn on."""
    monkeypatch.setenv("HUNTERAI_OLLAMA_MAX_CTX", "0")
    ollama_with(MODEL)
    respx.get(SERVER + "/api/system/status").mock(return_value=httpx.Response(200, json={"restart_needed": False, "stopped": []}))
    respx.get(SERVER + "/api/system/overhead").mock(return_value=httpx.Response(200, json={"tokens": 12000, "tools": 60, "on_demand": 0, "window": 16384}))
    verdict, words = said((await run(conn, "window"))[1], "local window")[0]
    assert verdict == "FAIL" and "(60 tools)" in words and "to 20,480 or more" in words
    respx.get(SERVER + "/api/system/overhead").mock(return_value=httpx.Response(200, json={"tokens": 8192, "tools": 60, "on_demand": 0, "window": 16384}))
    assert said((await run(conn, "window"))[1], "local window")[0][0] == "ok"            # exactly the room that is asked for


@respx.mock
async def test_without_a_local_model_server_the_window_is_not_measured(conn):
    nothing_answers()
    assert said((await run(conn, "window"))[1], "local window") == [("skip", "Ollama does not answer")]


async def test_the_server_counts_what_a_new_chat_sends_before_the_conversation(client):
    from hunterai.tools import registry as tools
    from hunterai.tools.base import Tool
    first = (await client.get("/api/system/overhead")).json()
    own = len([t for t in tools.all() if t.expose and t.name not in ("mcp_find", "mcp_call")])
    assert first["tools"] > 20 and first["on_demand"] == 0 and first["window"] == 16384
    assert 4000 < first["tokens"] < 12000

    async def handler(args, ctx):
        return None
    for n in range(20):          # a large MCP server: more tools than are listed in every request
        tools.register(Tool(f"big_tool_{n}", "does one thing with a long description " * 8,
                            {"type": "object", "properties": {"x": {"type": "string"}}}, "read", handler, source="mcp:big"))
    try:
        later = (await client.get("/api/system/overhead")).json()
        assert (later["tools"], later["on_demand"]) == (first["tools"] + 2, 20)        # mcp_find and mcp_call take their place
        await client.put("/api/settings", json={"mcp_on_demand": False})
        listed = (await client.get("/api/system/overhead")).json()
        assert (listed["tools"], listed["on_demand"]) == (first["tools"] + 20, 0)
        assert listed["tokens"] > later["tokens"] > first["tokens"]                     # on demand costs less than listing them
    finally:
        tools.unregister_source("mcp:big")
    assert own > 0


# ---- embeddings, Qdrant, SearXNG

@respx.mock
async def test_the_embedding_model_answers_with_numbers(conn, monkeypatch):
    monkeypatch.delenv("HUNTERAI_FAKE_EMBED", raising=False)
    ollama_with(MODEL, "bge-m3:latest")
    respx.post(OLLAMA + "/api/embed").mock(return_value=httpx.Response(200, json={"embeddings": [[0.1] * 1024]}))
    assert said((await run(conn, "embeddings"))[1], "embeddings") == [("ok", "bge-m3 answers with 1,024 numbers for a text")]


@respx.mock
async def test_a_missing_embedding_model_is_a_note_with_the_command(conn, monkeypatch):
    monkeypatch.delenv("HUNTERAI_FAKE_EMBED", raising=False)
    ollama_with(MODEL)
    assert said((await run(conn, "embeddings"))[1], "embeddings") == [
        ("note", "bge-m3 is not installed: the knowledge base needs it (`ollama pull bge-m3`)")]


@respx.mock
async def test_qdrant_that_answers_names_its_collections(conn):
    respx.get(QDRANT + "/collections").mock(return_value=httpx.Response(200, json={"result": {"collections": [{"name": "a"}, {"name": "b"}]}}))
    assert said((await run(conn, "qdrant"))[1], "Qdrant") == [("ok", f"answers at {QDRANT}, 2 collections")]


@respx.mock
async def test_qdrant_that_was_in_use_and_does_not_answer_fails_with_the_script(conn):
    nothing_answers()
    assert said((await run(conn, "qdrant"))[1], "Qdrant") == [("skip", f"no answer at {QDRANT} (it was not in use here)")]
    await in_use(conn, "qdrant")
    assert said((await run(conn, "qdrant"))[1], "Qdrant") == [("FAIL", f"no answer at {QDRANT}: run scripts/qdrant.sh (it needs Docker)")]


@respx.mock
async def test_searxng_is_asked_a_real_search(conn):
    respx.get(SEARX + "/search").mock(return_value=httpx.Response(200, json={"results": [
        {"title": "a", "url": "https://a.example", "content": "x"}, {"title": "b", "url": "https://b.example"}]}))
    assert said((await run(conn, "searxng"))[1], "SearXNG") == [("ok", f"answers at {SEARX}: 2 results for a test search")]


@respx.mock
async def test_searxng_that_refuses_the_format_the_tool_needs_fails_with_the_script(conn):
    respx.get(SEARX + "/search").mock(return_value=httpx.Response(403, text="forbidden"))
    code, lines = await run(conn, "searxng")
    assert said(lines, "SearXNG") == [("FAIL", "SearXNG refused the JSON format. Run scripts/searxng.sh start to write its settings.")]
    assert code == 1


@respx.mock
async def test_searxng_that_finds_nothing_is_said_as_that(conn):
    respx.get(SEARX + "/search").mock(return_value=httpx.Response(200, json={"results": []}))
    assert said((await run(conn, "searxng"))[1], "SearXNG") == [
        ("FAIL", f"answers at {SEARX}, but a test search found nothing: its search engines do not answer it (see `docker logs hunterai-searxng`)")]


@respx.mock
async def test_searxng_that_was_not_in_use_and_does_not_answer_is_skipped(conn):
    nothing_answers()
    assert said((await run(conn, "searxng"))[1], "SearXNG") == [("skip", f"no answer at {SEARX} (it was not in use here)")]
    await in_use(conn, "searxng")
    assert said((await run(conn, "searxng"))[1], "SearXNG") == [
        ("FAIL", f"SearXNG is not running at {SEARX}. Start it with scripts/searxng.sh start.")]


# ---- the owner's proxy

async def test_without_a_proxy_the_proxy_checks_are_skipped(conn):
    code, lines = await run(conn, "proxy")
    assert said(lines, "proxy for targets") == [("skip", "none is set")]
    assert said(lines, "proxy for models") == [("skip", "none is set")]
    assert code == 0


async def test_a_proxy_that_carries_a_request_names_what_sites_see(conn, monkeypatch):
    up = upstream.parse("socks5://hunter:s3cret@proxy.example:1080")

    async def get(conn_):
        return up

    async def probe(given):
        assert given is up
        return {"ok": True, "through": "socks5://proxy.example:1080", "ms": 420, "ip": "185.1.2.3", "country": "FR", "error": ""}

    async def direct():
        return "112.200.1.9", "PH"

    async def by_variables(given):
        return "185.1.2.3"
    monkeypatch.setattr(upstream, "get", get)
    monkeypatch.setattr(hop, "probe", probe)
    monkeypatch.setattr(smoke, "_direct", direct)
    monkeypatch.setattr(smoke, "_by_variables", by_variables)
    code, lines = await run(conn, "proxy")
    assert said(lines, "proxy for targets") == [
        ("ok", "sites see 185.1.2.3 (FR) through socks5://proxy.example:1080, in 420 ms; without it they see 112.200.1.9 (PH)"),
        ("ok", "a command started with the proxy variables comes out at 185.1.2.3 too")]
    assert code == 0
    assert "s3cret" not in "\n".join(lines)


async def test_a_proxy_that_changes_nothing_is_said(conn, monkeypatch):
    up = upstream.parse("http://proxy.example:8080")

    async def get(conn_):
        return up

    async def probe(given):
        return {"ok": True, "through": "http://proxy.example:8080", "ms": 80, "ip": "112.200.1.9", "country": "PH", "error": ""}

    async def direct():
        return "112.200.1.9", "PH"

    async def by_variables(given):
        return "112.200.1.9"
    monkeypatch.setattr(upstream, "get", get)
    monkeypatch.setattr(hop, "probe", probe)
    monkeypatch.setattr(smoke, "_direct", direct)
    monkeypatch.setattr(smoke, "_by_variables", by_variables)
    lines = (await run(conn, "proxy"))[1]
    assert said(lines, "proxy for targets")[0] == (
        "note", "sites see 112.200.1.9 (PH) through http://proxy.example:8080, in 80 ms: the same address as without it")


async def test_a_proxy_that_does_not_carry_a_request_fails_with_its_reason(conn, monkeypatch):
    up = upstream.parse("socks5://proxy.example:1080")

    async def get(conn_):
        return up

    async def probe(given):
        return {"ok": False, "through": "socks5://proxy.example:1080", "ms": 10000, "ip": "", "country": "", "error": "the proxy refused the login"}
    monkeypatch.setattr(upstream, "get", get)
    monkeypatch.setattr(hop, "probe", probe)
    code, lines = await run(conn, "proxy")
    assert said(lines, "proxy for targets") == [("FAIL", "socks5://proxy.example:1080 did not carry a request: the proxy refused the login")]
    assert code == 1


async def test_a_command_that_comes_out_elsewhere_than_the_proxy_fails(conn, monkeypatch):
    up = upstream.parse("socks5://proxy.example:1080")

    async def get(conn_):
        return up

    async def probe(given):
        return {"ok": True, "through": "socks5://proxy.example:1080", "ms": 420, "ip": "185.1.2.3", "country": "FR", "error": ""}

    async def direct():
        return "112.200.1.9", "PH"

    async def by_variables(given):
        return "112.200.1.9"
    monkeypatch.setattr(upstream, "get", get)
    monkeypatch.setattr(hop, "probe", probe)
    monkeypatch.setattr(smoke, "_direct", direct)
    monkeypatch.setattr(smoke, "_by_variables", by_variables)
    code, lines = await run(conn, "proxy")
    assert said(lines, "proxy for targets")[1] == (
        "FAIL", "a command started with the proxy variables comes out at 112.200.1.9, not at 185.1.2.3: it does not go through the proxy")
    assert code == 1


# ---- the model providers

class Answers:
    """A provider with a key that answers a short request."""
    supports_tools = True

    def __init__(self, name, fail=None):
        self.name, self.fail, self.asked = name, fail, []

    def configured(self):
        return True

    async def models(self):
        from hunterai.providers.base import ModelInfo
        return [ModelInfo(id=f"{self.name}-small", label="small")]

    async def stream(self, model, system, messages, options, tools=None):
        self.asked.append((model, options.max_tokens))
        if self.fail:
            raise self.fail
        yield TextDelta("OK")
        yield Usage(12, 1)
        yield Stop("end")


def providers(monkeypatch, *given):
    monkeypatch.setattr(smoke, "_cloud_providers", lambda: list(given))


async def test_a_provider_with_a_key_is_not_called_unless_asked(conn, monkeypatch):
    """A call costs tokens: the owner asks for it."""
    openai = Answers("openai")
    providers(monkeypatch, openai)
    code, lines = await run(conn, "providers")
    assert said(lines, "openai") == [("skip", "ready (a key or a login is there); not called: run with --calls to send it one short request")]
    assert openai.asked == [] and code == 0


async def test_with_calls_each_provider_is_sent_one_short_request(conn, monkeypatch):
    openai, xai = Answers("openai"), Answers("xai", fail=ProviderError("auth", "HTTP 401: bad key", 401))
    providers(monkeypatch, openai, xai)
    code, lines = await run(conn, "providers", calls=True)
    verdict, words = said(lines, "openai")[0]
    assert verdict == "ok" and words.startswith("openai-small answered in ") and words.endswith("s (12 tokens in, 1 out)")
    assert openai.asked == [("openai-small", 16)]
    assert said(lines, "xai") == [("FAIL", "xai: HTTP 401: bad key. The provider did not accept the key: check it under Settings.")]
    assert code == 1


async def test_without_any_provider_that_is_ready_the_check_says_so(conn, monkeypatch):
    providers(monkeypatch)
    assert said((await run(conn, "providers"))[1], "providers") == [("note", "no cloud provider has a key or a login here")]


# ---- the whole run

@respx.mock
async def test_the_run_ends_with_a_count_and_fails_when_one_check_fails(conn, monkeypatch):
    nothing_answers()
    providers(monkeypatch)
    await in_use(conn, "qdrant")
    code, lines = await run(conn)
    assert code == 1
    assert lines[-1] == "1 failed, 0 ok, 12 skipped or noted. Each FAIL line says what to do."
    assert [verdict for verdict, _ in said(lines, "Qdrant")] == ["FAIL"]


@respx.mock
async def test_a_run_without_a_failure_ends_in_order(conn, monkeypatch):
    nothing_answers()
    providers(monkeypatch)
    code, lines = await run(conn)
    assert code == 0 and lines[-1] == "Nothing failed: 0 ok, 13 skipped or noted."
    assert said(lines, "long chat") == [("skip", "only with --deep (the local model reads a long chat: minutes on a slow machine)")]


async def test_only_the_named_checks_are_made_and_an_unknown_name_is_refused(conn, monkeypatch):
    providers(monkeypatch)
    code, lines = await run(conn, "providers")
    assert all(line.split()[1] == "providers" for line in lines[:-1])
    with pytest.raises(smoke.Refused) as refused:
        await run(conn, "everything")
    assert str(refused.value) == ("There is no check called everything. The checks are: server, ollama, local, window, colibri, "
                                  "llamacpp, embeddings, qdrant, searxng, proxy, providers, chat")


def test_the_long_chat_is_only_made_when_asked_for():
    assert smoke.CHECKS["chat"].deep is True and not any(c.deep for name, c in smoke.CHECKS.items() if name != "chat")


@respx.mock
async def test_the_deep_run_has_the_local_model_answer_a_long_chat_in_place_of_a_model_that_cannot_be_reached(conn):
    """The reported case, whole: the chat's own model fails, the local one answers, and it has to know the chat."""
    ollama_with(MODEL)
    respx.post(OLLAMA + "/api/show").mock(return_value=httpx.Response(200, json={
        "capabilities": ["completion", "tools"], "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960}}))
    respx.post(OLLAMA + "/api/chat").mock(side_effect=long_chat_model(keeps_the_start=True))
    home = config.home()
    code, lines = await run(conn, "chat", deep=True)
    got = said(lines, "long chat")
    assert got[0][0] == "ok", got
    assert got[0][1].startswith(f"{MODEL} answered a chat of 27 messages in place of a model that could not be reached: it knew the "
                                "word from two turns before and what was found at the start of the chat (")
    assert got[0][1].endswith("messages were summarized to fit)")
    assert code == 0
    # Nothing of the check is left in place: the chat's own provider, the data folder, and no chat in the owner's database.
    assert type(registry.get("abliteration")).__name__ != "_Unreachable" and config.home() == home
    assert (await (await conn.execute("select count(*) from chats")).fetchone())[0] == 0


def long_chat_model(keeps_the_start: bool, knows_the_chat: bool = True):
    """A stand-in local model for the long chat: its summary carries what was found at the start, or not; its answer
    names what it can find in what it was sent."""
    def reply(request):
        body = json.loads(request.content)
        text = "\n".join(m.get("content") or "" for m in body["messages"])
        if body["messages"][0]["content"] == context.SUMMARY_SYSTEM:
            says = "The admin panel was found at /zx-admin-7731." if keeps_the_start else "The user and the assistant looked at a login."
        else:
            found = [x for x in ("/zx-admin-7731", "PAPAYA-7731") if x in text and knows_the_chat]
            says = "They are: " + " and ".join(found) if found else "I do not know what you mean. What are we working on?"
        return httpx.Response(200, text=line({"message": {"role": "assistant", "content": says}, "done": False})
                              + line({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                                      "prompt_eval_count": 9000, "eval_count": 20}))
    return reply


def a_local_model_for_the_long_chat(**how):
    ollama_with(MODEL)
    respx.post(OLLAMA + "/api/show").mock(return_value=httpx.Response(200, json={
        "capabilities": ["completion", "tools"], "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960}}))
    respx.post(OLLAMA + "/api/chat").mock(side_effect=long_chat_model(**how))


@respx.mock
async def test_a_summary_that_lost_the_start_of_the_chat_is_a_note_and_not_a_failure(conn):
    """A small model can write a summary that drops a detail. The chat still reached it."""
    a_local_model_for_the_long_chat(keeps_the_start=False)
    code, lines = await run(conn, "chat", deep=True)
    verdict, words = said(lines, "long chat")[0]
    assert (verdict, code) == ("note", 0)
    assert "it knew the word from two turns before, but not what was found at the start of the chat: the summary it wrote" in words


@respx.mock
async def test_a_local_model_that_does_not_know_the_long_chat_fails(conn):
    a_local_model_for_the_long_chat(keeps_the_start=True, knows_the_chat=False)
    code, lines = await run(conn, "chat", deep=True)
    verdict, words = said(lines, "long chat")[0]
    assert (verdict, code) == ("FAIL", 1)
    assert words.startswith(f"{MODEL} answered a chat of 27 messages without the word from two turns before. It answered: "
                            '"I do not know what you mean. What are we working on?"')


async def test_the_command_reads_its_switches(monkeypatch):
    seen = {}

    async def run_(conn, only=None, deep=False, calls=False, out=print):
        seen.update(only=only, deep=deep, calls=calls)
        return 0
    monkeypatch.setattr(smoke, "run", run_)
    assert await smoke.amain(["--deep", "--calls", "--only", "ollama,local"]) == 0
    assert seen == {"only": ["ollama", "local"], "deep": True, "calls": True}
    assert await smoke.amain([]) == 0 and seen == {"only": None, "deep": False, "calls": False}
