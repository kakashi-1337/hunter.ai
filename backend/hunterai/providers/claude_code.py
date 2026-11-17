"""Adapter that drives the local `claude` CLI (Claude Code) non-interactively.

Claude is reached through the CLI's own login (a Claude subscription), not an API key:
HunterAI never reads or holds that login. Each call runs

    claude -p --output-format stream-json --verbose --include-partial-messages
           --model M --system-prompt S --tools "" --strict-mcp-config --setting-sources ""
           --no-session-persistence --disable-slash-commands

in a fresh empty folder, with the conversation on stdin, and reads the JSON lines it prints
(checked against Claude Code 2.1.325). No tools, no MCP servers, no settings, hooks, or
plugins of the owner's own Claude Code are loaded, and nothing is saved as a session.

No tools for HunterAI either: chats on this provider get the no-tools notice. Its use is
as the decision model, or for a chat that only needs answers. Every call starts a process
(seconds, not milliseconds) and counts against the subscription's usage limits. Whether a
subscription may be used this way is a matter of Anthropic's terms, which the owner checks.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile

from .. import procs
from .base import ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta, Usage, WireMessage, cli_env, way_out
from .codex import flatten_conversation

EXECUTABLE = "claude"
MAX_ARGUMENT = 100_000          # bytes; Linux refuses a single argument over 128 KiB
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_MODELS = (("haiku", "Claude Haiku (subscription, via the claude CLI)"),
           ("sonnet", "Claude Sonnet (subscription, via the claude CLI)"),
           ("opus", "Claude Opus (subscription, via the claude CLI)"))
_LOGIN = ("not logged in", "/login", "invalid api key", "authentication", "oauth token")
_LIMIT = ("usage limit", "rate limit", "limit reached", "overloaded")


def _failure(text: str) -> ProviderError:
    low = text.lower()
    if any(s in low for s in _LOGIN):
        return ProviderError("auth", "the claude CLI is not logged in: run `claude` in a terminal and log in, then try again",
                             complete=True)
    if any(s in low for s in _LIMIT):
        return ProviderError("rate_limit", f"claude: {text[:300]}")
    return ProviderError("unavailable", f"claude failed: {text[:300]}")


class ClaudeCodeProvider:
    name = "claude_code"

    def configured(self) -> bool:
        return shutil.which(EXECUTABLE) is not None

    async def models(self) -> list[ModelInfo]:
        return [ModelInfo(id=i, label=label, options=["effort"]) for i, label in _MODELS]

    async def stream(self, model: str, system: str, messages: list[WireMessage], options: Options):
        exe = shutil.which(EXECUTABLE)
        if not exe:
            raise ProviderError("unavailable", "the claude CLI is not installed or not on PATH")
        args = ["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--model", model,
                "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                "--disable-slash-commands"]
        if options.effort in _EFFORTS:
            args += ["--effort", options.effort]
        system = system.strip()
        in_argument = bool(system) and len(system.encode()) <= MAX_ARGUMENT
        if in_argument:
            args += ["--system-prompt", system]
        prompt = flatten_conversation("" if in_argument else system, messages)
        workdir = tempfile.mkdtemp(prefix="hunterai-claude-")
        proc = None
        usage, partial, emitted, failure = Usage(0, 0), False, False, None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *args, cwd=workdir,
                env=cli_env(procs.child_env(), await way_out()),     # no API key of ours: the CLI uses its own login
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024, start_new_session=True)
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                kind = ev.get("type")
                if kind == "stream_event":
                    delta = ((ev.get("event") or {}).get("delta")) or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        partial = emitted = True
                        yield TextDelta(delta["text"])
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        partial = True
                        yield ThinkingDelta(delta["thinking"])
                elif kind == "assistant" and not partial:        # no pieces were sent: take the whole message
                    for block in ((ev.get("message") or {}).get("content")) or []:
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                            emitted = True
                            yield TextDelta(block["text"])
                elif kind == "result":
                    u = ev.get("usage") or {}
                    usage = Usage(sum(int(u.get(k) or 0) for k in
                                      ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
                                  int(u.get("output_tokens") or 0))
                    if ev.get("is_error"):
                        failure = str(ev.get("result") or ev.get("subtype") or "the CLI reported an error")
            stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
            code = await proc.wait()
            if failure or (code != 0 and not emitted):
                raise _failure(failure or stderr[-300:] or f"exit code {code}")
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)      # the whole group: helpers the CLI started die with it
                except ProcessLookupError:
                    pass
                await proc.wait()
            shutil.rmtree(workdir, ignore_errors=True)
        yield usage
        yield Stop("end")


def make() -> ClaudeCodeProvider:
    return ClaudeCodeProvider()
