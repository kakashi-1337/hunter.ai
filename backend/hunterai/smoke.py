"""Checks against the real services of this install: Ollama and the local model, colibri, Qdrant, SearXNG, the
owner's proxy, the model providers, and a running HunterAI. Run with scripts/smoke.sh after an update, or when something
does not work. The tests of HunterAI use stand-ins for all of these; this is where the real ones are asked.

Nothing is sent to a target and nothing of the owner's is changed: the checks read the settings, ask the services
on this machine, and (only with --calls) send each cloud provider one short request. The long chat of --deep
lives in a database of its own that is thrown away.

    scripts/smoke.sh                  every check that costs nothing
    scripts/smoke.sh --deep           and a long chat answered by the local model in place of an unreachable one
    scripts/smoke.sh --calls          and one short request to each cloud provider that has a key (a few tokens),
                                      and one to colibri when it is set up
    scripts/smoke.sh --only local,proxy

Exit code: 0 when nothing failed, 1 when a check failed, 2 when the command line is not understood."""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from . import config, db, engine, hop, problems, upstream
from .api import system as system_api
from .kb import embed
from .providers import colibri, ollama, registry
from .providers.base import ModelInfo, Options, ProviderError, Stop, TextDelta, Usage, WireMessage
from .repo import settings
from .tools.base import ToolError
from .types import TextBlock

WIDTH = 18                  # of the column that names the check
ASK = httpx.Timeout(5.0, connect=2.0)
WORD = "PAPAYA-7731"        # what the local model is told and asked for again
EARLY = "/zx-admin-7731"    # what the long chat finds in its first message
ROOM = system_api.ROOM      # tokens a local window should have left beside the rules and the tools
CLI = ("codex", "claude_code")      # providers that are a program on this machine, not a key


class Refused(Exception):
    """The command line asks for something there is not."""


@dataclass
class Result:
    name: str
    status: str             # "ok" | "FAIL" | "skip" | "note"
    said: str

    def line(self) -> str:
        return f"{self.status:<5} {self.name:<{WIDTH}}  {self.said}"


@dataclass
class Env:
    """What the checks share: the owner's database, the switches, and what was already asked."""
    conn: object
    deep: bool = False
    calls: bool = False
    known: dict = field(default_factory=dict)

    async def once(self, key: str, ask: Callable[[], Awaitable]):
        if key not in self.known:
            self.known[key] = await ask()
        return self.known[key]

    async def server(self) -> dict | None:
        return await self.once("server", lambda: _get(f"{_server()}/api/system/status"))

    async def ollama(self) -> tuple[str, list[str]] | None:
        """(version, the names of the installed models), or None when Ollama does not answer."""
        async def ask():
            version, tags = await _get(ollama.host() + "/api/version"), await _get(ollama.host() + "/api/tags")
            if tags is None:
                return None
            names = [str(m.get("name")) for m in tags.get("models") or [] if isinstance(m, dict) and m.get("name")]
            return str((version or {}).get("version") or "?"), names
        return await self.once("ollama", ask)

    async def in_use(self, name: str) -> bool:
        last = await system_api._answered(self.conn, name)
        return last is not None and time.time() - last <= system_api.IN_USE


def _server() -> str:
    return f"http://127.0.0.1:{config.port()}"


async def _get(url: str) -> dict | None:
    """The JSON a service on this machine answers with; None when it does not answer or answers with a failure."""
    try:
        async with httpx.AsyncClient(timeout=ASK, trust_env=False) as client:
            r = await client.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code >= 400:
        return None
    try:
        body = r.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _has(names: list[str], model: str) -> bool:
    return any(n in (model, model + ":latest") or n + ":latest" == model for n in names)


# ---- HunterAI itself

async def _check_server(env: Env) -> list[Result]:
    status, base = await env.server(), _server()
    if status is None:
        return [Result("HunterAI", "skip", f"not running at {base} (the other checks do not need it)")]
    if status.get("restart_needed") is not False:       # absent: a server from before it could tell
        return [Result("HunterAI", "FAIL", f"runs at {base} on code from before the last change: press Ctrl+C where it "
                                           "runs, then start it again (scripts/start.sh)")]
    out = [Result("HunterAI", "ok", f"runs at {base} on the code that is on disk")]
    stopped = [s.get("label") for s in status.get("stopped") or [] if isinstance(s, dict) and s.get("label")]
    if stopped:
        out.append(Result("HunterAI", "note", "it says these are not working: " + ", ".join(stopped)))
    return out


# ---- Ollama and the local model

async def _down(env: Env, name: str, label: str, where: str, fix: str) -> Result:
    """A service that does not answer: a failure when it was in use here, otherwise not this install's concern."""
    if await env.in_use(name):
        return Result(label, "FAIL", f"no answer at {where}: {fix}")
    return Result(label, "skip", f"no answer at {where} (it was not in use here)")


async def _check_ollama(env: Env) -> list[Result]:
    found = await env.ollama()
    if found is None:
        return [await _down(env, "ollama", "Ollama", ollama.host(), "start it with `ollama serve`")]
    version, names = found
    return [Result("Ollama", "ok", f"{version} at {ollama.host()}, {len(names)} model{'' if len(names) == 1 else 's'}")]


async def _fallback(env: Env) -> str:
    return await settings.get(env.conn, "fallback_model", engine.DEFAULT_FALLBACK_MODEL)


async def _answer(provider, model: str, system: str, messages: list[WireMessage], options: Options) -> tuple[str, Usage, float]:
    """(the text, the tokens, the seconds) of one call."""
    began, text, used = time.monotonic(), "", Usage(0, 0)
    async for ev in provider.stream(model, system, messages, options):
        if isinstance(ev, TextDelta):
            text += ev.text
        elif isinstance(ev, Usage):
            used = ev
        elif isinstance(ev, Stop):
            break
    return text.strip(), used, time.monotonic() - began


def _quoted(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


async def _check_local(env: Env) -> list[Result]:
    found = await env.ollama()
    if found is None:
        return [Result("local model", "skip", "Ollama does not answer")]
    model, chat_models = await _fallback(env), [n for n in found[1] if not ollama._is_embedder(n)]
    if not chat_models:
        return [Result("local model", "note", "Ollama has no chat model: the fallback cannot answer until one is pulled "
                                              f"(`ollama pull {model}`)")]
    if not _has(chat_models, model):
        return [Result("local model", "FAIL", f"{model}, the fallback model, is not installed: run `ollama pull {model}`, or "
                                              "choose an installed model as the fallback under Settings (installed: "
                                              f"{', '.join(chat_models[:5])})")]
    provider = registry.get("ollama")
    thinks = [v for v in (await provider.thinking_values(model) or []) if v is not False]
    options = Options(max_tokens=64, thinking=False if thinks else None)
    chat = [WireMessage("user", [TextBlock(text=f"The code word is {WORD}. Remember it.")]),
            WireMessage("assistant", [TextBlock(text=f"Noted: {WORD}.")]),
            WireMessage("user", [TextBlock(text="What is the code word? Reply with the code word only.")])]
    try:
        text, used, seconds = await _answer(provider, model, "You are a careful assistant.", chat, options)
    except ProviderError as e:
        e.provider, e.model = "ollama", model
        return [Result("local model", "FAIL", problems.model_call(e))]
    if WORD.lower() not in text.lower():
        return [Result("local model", "FAIL", f'{model} did not repeat a word from two messages before. It answered: "{_quoted(text)}" '
                                              "Either the conversation does not reach it, or the model is too small to follow one.")]
    out = [Result("local model", "ok", f"{model} repeated a word from two messages before, in {seconds:.1f} s "
                                       f"({used.input_tokens:,} tokens in, {used.output_tokens:,} out)")]
    window, limit = await provider.window_for(model, Options()), provider._limit.get(model)
    words = f"it gets a window of {window:,} tokens" + (f" of its own {limit:,}" if limit else "")
    loaded = next((m for m in (await _get(ollama.host() + "/api/ps") or {}).get("models") or []
                   if isinstance(m, dict) and _has([str(m.get("name"))], model)), None)
    if loaded:
        words += f"; Ollama runs it {ollama.processor(loaded)}"
        if loaded.get("context_length"):
            words += f" with a window of {int(loaded['context_length']):,}"
    out.append(Result("local model", "ok", words))
    return out


async def _overhead(env: Env) -> tuple[int, int, str]:
    """(the tokens a new chat sends before a word of the conversation, the window for local models, which tools
    were counted). The running server counts the tools of the MCP servers too; without it they are left out."""
    counted = await _get(f"{_server()}/api/system/overhead") if await env.server() is not None else None
    if counted and counted.get("tokens"):
        tokens, tools, later, window = (int(counted.get(k) or 0) for k in ("tokens", "tools", "on_demand", "window"))
        return tokens, window, f"{tools:,} tools" + (f", and {later:,} more that are loaded on demand" if later else "")
    from .tools import native
    from .tools import registry as tools_registry
    if not tools_registry.all():
        native.register_all()
    mine = await system_api.overhead(env.conn)
    return mine["tokens"], mine["window"], f"{mine['tools']:,} tools, without those of MCP servers: start HunterAI to count them"


async def _check_window(env: Env) -> list[Result]:
    """The first request of a chat holds HunterAI's rules and every tool definition before a word of the
    conversation: with many MCP tools that alone can be more than a local window."""
    if await env.ollama() is None:
        return [Result("local window", "skip", "Ollama does not answer")]
    tokens, window, which = await _overhead(env)
    said = f"a new chat sends about {tokens:,} tokens of rules and tools ({which}); the window for local models is {window:,}"
    if tokens + ROOM <= window:
        return [Result("local window", "ok", said)]
    enough = -(-(tokens + ROOM) // 4096) * 4096
    return [Result("local window", "FAIL", f'{said}. Raise "Context window for local models" in Settings to {enough:,} or more '
                                           "(a larger window needs more memory), or switch off the MCP servers you do not need")]


# ---- colibri

COLIBRI_START = f"{colibri.START} in the colibri folder"


async def _check_colibri(env: Env) -> list[Result]:
    """colibri, when the owner gave its address: whether it answers, what it serves, whether its context holds the
    rules and tools of a new chat with room for the chat, and with --calls whether it answers a request."""
    provider = registry.get(colibri.NAME)
    seen = await provider.served(fresh=True)
    if seen.state == "off":
        return [Result("colibri", "skip", "no address is set for it (Settings, colibri)")]
    if seen.state == "down":
        if await settings.get(env.conn, "default_provider") == colibri.NAME:
            return [Result("colibri", "FAIL", f"{colibri.why(seen)}, and new chats are set to use it: start it ({COLIBRI_START})")]
        return [Result("colibri", "note", f"{colibri.why(seen)}: start it when you want to use it ({COLIBRI_START})")]
    if seen.state != "ok":          # something answers there, and not as colibri set up for HunterAI would
        return [Result("colibri", "FAIL", f"{colibri.why(seen)} (the address is {seen.at}: Settings, colibri)")]
    model = seen.models[0]
    reads = "reads pictures" if model.supports_images else "reads text only"
    window = f"a context of {model.context_window:,} tokens" if model.context_window else "a context it does not name"
    out = [Result("colibri", "ok", f"{model.id} at {seen.at}, {window}, {reads}")]
    tokens, _, _ = await _overhead(env)
    said = f"a new chat sends about {tokens:,} tokens of rules and tools"
    enough = colibri.context_to_start(tokens + ROOM, model.context_window)
    if model.context_window and enough:
        out.append(Result("colibri", "FAIL", f"{said}, which leaves too little of the context for the chat: start colibri with a "
                                             f"larger one ({colibri.START} --ctx {enough} in the colibri folder; a larger context "
                                             "needs more memory)"))
    elif model.context_window and tokens + ROOM > model.context_window:
        out.append(Result("colibri", "FAIL", f"{said}, which leaves too little of the context for the chat, and colibri offers "
                                             "no larger one: switch off the MCP servers you do not need"))
    elif model.context_window:
        out.append(Result("colibri", "ok", f"{said}, which leaves room for the chat"))
    if not env.calls:
        return [*out, Result("colibri", "skip", "not called: run with --calls to send it one short request")]
    try:
        text, used, seconds = await _answer(provider, model.id, "", [WireMessage("user", [TextBlock(text="Reply with the word OK.")])],
                                            Options(max_tokens=16, thinking=False))     # the tokens are for the word
        out.append(Result("colibri", "ok", f"{model.id} answered in {seconds:.1f} s ({used.input_tokens:,} tokens in, "
                                           f"{used.output_tokens:,} out)"))
    except ProviderError as e:
        e.provider, e.model = colibri.NAME, model.id
        out.append(Result("colibri", "FAIL", problems.model_call(e)))
    return out


# ---- embeddings, Qdrant, SearXNG

async def _check_embeddings(env: Env) -> list[Result]:
    found = await env.ollama()
    if found is None:
        return [Result("embeddings", "skip", "Ollama does not answer")]
    model = embed.DEFAULT_EMBEDDER.partition(":")[2]
    if not _has(found[1], model):
        return [Result("embeddings", "note", f"{model} is not installed: the knowledge base needs it (`ollama pull {model}`)")]
    try:
        vectors = await embed.OllamaEmbedder(model, lock_timeout=30).embed(["HunterAI smoke check"])
    except embed.EmbedError as e:
        return [Result("embeddings", "FAIL", f"{model} did not answer: {e}")]
    return [Result("embeddings", "ok", f"{model} answers with {len(vectors[0]):,} numbers for a text")]


async def _check_qdrant(env: Env) -> list[Result]:
    base = config.qdrant_url()
    found = await _get(base + "/collections")
    if found is None:
        return [await _down(env, "qdrant", "Qdrant", base, "run scripts/qdrant.sh (it needs Docker)")]
    n = len(((found.get("result") or {}).get("collections")) or [])
    return [Result("Qdrant", "ok", f"answers at {base}, {n} collection{'' if n == 1 else 's'}")]


async def _check_searxng(env: Env) -> list[Result]:
    from .tools.search import SearxngProvider
    base = config.searxng_url()
    try:
        found = await SearxngProvider().search("open source software", limit=5)
    except ToolError as e:
        if "is not running" in str(e) and not await env.in_use("searxng"):
            return [Result("SearXNG", "skip", f"no answer at {base} (it was not in use here)")]
        return [Result("SearXNG", "FAIL", str(e))]         # the tool's own words say what to do
    if not found:
        return [Result("SearXNG", "FAIL", f"answers at {base}, but a test search found nothing: its search engines do not answer "
                                          "it (see `docker logs hunterai-searxng`)")]
    return [Result("SearXNG", "ok", f"answers at {base}: {len(found)} result{'' if len(found) == 1 else 's'} for a test search")]


# ---- the owner's proxy

async def _direct() -> tuple[str, str]:
    """(address, country) that sites see from this machine without a proxy; empty when no page said."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), trust_env=False) as client:
            for url in hop._probes():
                try:
                    r = await client.get(url)
                except httpx.HTTPError:
                    continue
                ip, country = hop.where(r.text) if r.status_code == 200 else ("", "")
                if ip:
                    return ip, country
    except OSError:
        pass
    return "", ""


async def _by_variables(up) -> str:
    """The address sites see from a command that was started the way the shell tool starts one: with the proxy
    variables that point at the local hop. Empty when curl is not there or no page said."""
    curl = shutil.which("curl")
    if not curl:
        return ""
    env = {**os.environ, **upstream.variables(await hop.address(up))}
    for url in hop._probes():
        try:
            proc = await asyncio.create_subprocess_exec(curl, "-s", "-m", "15", url, env=env, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except (OSError, asyncio.TimeoutError):
            continue
        ip, _ = hop.where(out.decode("utf-8", "replace"))
        if ip:
            return ip
    return ""


async def _probe(label: str, up, commands: bool) -> list[Result]:
    if up is None:
        return [Result(label, "skip", "none is set")]
    got = await hop.probe(up)
    if not got["ok"]:
        return [Result(label, "FAIL", f"{got['through']} did not carry a request: {got['error']}")]
    if not got["ip"]:
        return [Result(label, "note", f"{got['through']} carried a request in {got['ms']:,} ms; {got['error']}")]
    seen = f"sites see {got['ip']}" + (f" ({got['country']})" if got["country"] else "") + f" through {got['through']}, in {got['ms']:,} ms"
    ip, country = await _direct()
    if ip and ip == got["ip"]:
        out = [Result(label, "note", f"{seen}: the same address as without it")]
    else:
        out = [Result(label, "ok", seen + (f"; without it they see {ip}" + (f" ({country})" if country else "") if ip else ""))]
    if commands:
        came = await _by_variables(up)
        if came == got["ip"]:
            out.append(Result(label, "ok", f"a command started with the proxy variables comes out at {came} too"))
        elif came:
            out.append(Result(label, "FAIL", f"a command started with the proxy variables comes out at {came}, not at {got['ip']}: "
                                             "it does not go through the proxy"))
        else:
            out.append(Result(label, "note", "a command with the proxy variables could not be tried (curl is not installed, or "
                                             "no page said where the request came from)"))
    return out


async def _check_proxy(env: Env) -> list[Result]:
    try:
        models_via = upstream.of_providers()
    except upstream.ProxyError as e:
        return [*await _probe("proxy for targets", await upstream.get(env.conn), True),
                Result("proxy for models", "FAIL", f"the address in the .env file is not one: {e}")]
    try:
        return [*await _probe("proxy for targets", await upstream.get(env.conn), True),
                *await _probe("proxy for models", models_via, False)]
    finally:
        await hop.shutdown()


# ---- the model providers

def _cloud_providers() -> list:
    """Every provider that is ready to be called and is not the local one or a stand-in."""
    out = []
    for p in registry.all():
        try:
            if p.name not in ("ollama", colibri.NAME, "mock") and p.configured():
                out.append(p)
        except Exception:  # noqa: BLE001 - one broken provider must not hide the others
            pass
    return out


async def _check_providers(env: Env) -> list[Result]:
    ready = _cloud_providers()
    if not ready:
        return [Result("providers", "note", "no cloud provider has a key or a login here")]
    out = []
    for p in ready:
        if not env.calls:
            out.append(Result(p.name, "skip", "ready (a key or a login is there); not called: run with --calls to send it one "
                                              "short request"))
            continue
        try:
            listed = await p.models()
            model = listed[0].id if listed else ""
            text, used, seconds = await _answer(p, model, "", [WireMessage("user", [TextBlock(text="Reply with the word OK.")])],
                                                Options(max_tokens=16))
            out.append(Result(p.name, "ok", f"{model} answered in {seconds:.1f} s ({used.input_tokens:,} tokens in, "
                                            f"{used.output_tokens:,} out)"))
        except ProviderError as e:
            e.provider = e.provider or p.name
            out.append(Result(p.name, "FAIL", problems.model_call(e)))
        except Exception as e:  # noqa: BLE001 - a fault of the adapter is a finding too
            out.append(Result(p.name, "FAIL", f"{type(e).__name__}: {e}"))
    return out


# ---- the long chat

class _Unreachable:
    """The chat's own model for the long chat: it cannot be reached, so the local one answers in its place."""
    name = engine.DEFAULT_PROVIDER
    supports_tools = True

    def configured(self) -> bool:
        return True

    async def models(self) -> list[ModelInfo]:
        return [ModelInfo(id="unreachable", label="unreachable", context_window=262144)]

    async def stream(self, model, system, messages, options, tools=None):
        raise ProviderError("unavailable", "made unreachable for the smoke check", 503)
        yield       # a generator, like every provider's stream


async def _no_bytes(upload_id: str) -> bytes:
    return b""


async def _check_long_chat(env: Env) -> list[Result]:
    """The case that was reported, whole and through the engine: a chat that has gone on for a while, the chat's
    own model cannot be reached, the local model answers in its place, and it has to know the chat."""
    if not env.deep:
        return [Result("long chat", "skip", "only with --deep (the local model reads a long chat: minutes on a slow machine)")]
    found = await env.ollama()
    model = await _fallback(env)
    if found is None or not _has(found[1], model):
        return [Result("long chat", "skip", "Ollama does not answer" if found is None else f"{model} is not installed")]
    from .repo import chats, messages, runs
    from .tools import native
    kept = {k: os.environ.get(k) for k in ("HUNTERAI_HOME", "HUNTERAI_ANSWER_CHECK")}
    scratch = tempfile.mkdtemp(prefix="hunterai-smoke-")
    conn = None
    try:
        os.environ.update(HUNTERAI_HOME=scratch, HUNTERAI_ANSWER_CHECK="off")      # a database of its own, thrown away after
        conn = await db.connect()
        await db.migrate(conn)
        await settings.put(conn, "fallback_model", model)
        native.register_all()
        registry.override(engine.DEFAULT_PROVIDER, _Unreachable())
        chat = await chats.create(conn, None, "pentesting", engine.DEFAULT_PROVIDER, "unreachable", {})
        said = [("user", f"We are mapping the login of shop.example. First finding: the admin panel is at {EARLY}. Keep it in mind."),
                ("assistant", f"Noted: the admin panel is at {EARLY}.")]
        for n in range(1, 12):
            said += [("user", f"note {n}: " + "the login form posts to /session and sets a cookie. " * 55),
                     ("assistant", f"read {n}: " + "nothing new on that page, moving on to the next one. " * 55)]
        said += [("user", f"The code word of this session is {WORD}."), ("assistant", f"Understood: {WORD}."),
                 ("user", "Two questions. Which path did we find for the admin panel? What is the code word of this session? "
                          "Answer with both.")]
        parent = None
        for role, text in said:
            parent = (await messages.add(conn, chat.id, parent, role, [TextBlock(text=text)])).id
        began = time.monotonic()
        run_id, mid = await engine.start_run(db.connect, chat.id, parent, _no_bytes)
        await engine.wait(run_id)
        seconds = time.monotonic() - began
        run, msg = await runs.get(conn, run_id), await messages.get(conn, mid)
        if run.status != "done":
            return [Result("long chat", "FAIL", run.error or f"the run ended as {run.status}")]
        answer = " ".join(b.text for b in msg.blocks if b.type == "text")
        cut = next((b.text for b in msg.blocks if b.type == "notice"), "")
        summarized = next((s.detail["context"].get("messages_summarized") for s in run.steps
                           if isinstance(s.detail, dict) and isinstance(s.detail.get("context"), dict)), 0)
        took = f"{seconds:.0f} s" + (f"; {summarized} messages were summarized to fit" if summarized else "")
        if WORD.lower() not in answer.lower():
            return [Result("long chat", "FAIL", f"{model} answered a chat of {len(said)} messages without the word from two turns "
                                                f'before. It answered: "{_quoted(answer)}"' + (f" {cut}" if cut else ""))]
        head = f"{model} answered a chat of {len(said)} messages in place of a model that could not be reached: it knew the word from two turns before"
        if EARLY.lower() in answer.lower():
            return [Result("long chat", "ok", f"{head} and what was found at the start of the chat ({took})")]
        return [Result("long chat", "note", f"{head}, but not what was found at the start of the chat: the summary it wrote of the "
                                            f"early part did not carry it, which a small model can do ({took})")]
    finally:
        registry.override(engine.DEFAULT_PROVIDER, None)
        if conn is not None:
            await conn.close()
        for key, value in kept.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(scratch, ignore_errors=True)


# ---- the run

@dataclass
class Check:
    run: Callable[[Env], Awaitable[list[Result]]]
    deep: bool = False      # made only with --deep


CHECKS: dict[str, Check] = {
    "server": Check(_check_server), "ollama": Check(_check_ollama), "local": Check(_check_local), "window": Check(_check_window),
    "colibri": Check(_check_colibri),
    "embeddings": Check(_check_embeddings), "qdrant": Check(_check_qdrant), "searxng": Check(_check_searxng),
    "proxy": Check(_check_proxy), "providers": Check(_check_providers), "chat": Check(_check_long_chat, deep=True),
}


async def run(conn, only: list[str] | None = None, deep: bool = False, calls: bool = False, out=print) -> int:
    """Make the checks, say one line for each thing found, and return the exit code."""
    for name in only or []:
        if name not in CHECKS:
            raise Refused(f"There is no check called {name}. The checks are: {', '.join(CHECKS)}")
    env = Env(conn, deep, calls)
    counts = {"ok": 0, "FAIL": 0, "other": 0}
    for name, check in CHECKS.items():
        if only and name not in only:
            continue
        try:
            results = await check.run(env)
        except Exception as e:  # noqa: BLE001 - a check that breaks is a failure of that check, not of the run
            results = [Result(name, "FAIL", problems.own_failure(e, f"making the check {name}"))]
        for r in results:
            out(r.line())
            counts[r.status if r.status in counts else "other"] += 1
    if counts["FAIL"]:
        out(f"{counts['FAIL']} failed, {counts['ok']} ok, {counts['other']} skipped or noted. Each FAIL line says what to do.")
    else:
        out(f"Nothing failed: {counts['ok']} ok, {counts['other']} skipped or noted.")
    return 1 if counts["FAIL"] else 0


async def amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="scripts/smoke.sh", description="Checks against the real services of this install.")
    parser.add_argument("--deep", action="store_true", help="also have the local model answer a long chat (minutes on a slow machine)")
    parser.add_argument("--calls", action="store_true", help="also send one short request to each cloud provider that is ready (costs a few tokens)")
    parser.add_argument("--only", default="", help="comma-separated names of the checks to make: " + ", ".join(CHECKS))
    args = parser.parse_args(argv)
    conn = await db.connect()
    try:
        await db.migrate(conn)
        return await run(conn, only=[n.strip() for n in args.only.split(",") if n.strip()] or None, deep=args.deep, calls=args.calls)
    except Refused as e:
        print(e, file=sys.stderr)
        return 2
    finally:
        await conn.close()


def main() -> None:
    sys.exit(asyncio.run(amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
