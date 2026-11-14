"""Adapter that drives the local `codex` CLI (OpenAI Codex) non-interactively.

Codex is reached through the CLI's own login rather than an API key. Each call runs

    codex exec --json --sandbox read-only --skip-git-repo-check [-c model_reasoning_effort="X"] -

in a fresh empty temp directory, with the conversation on stdin, and reads the JSON
event lines it prints. NOTE: these flags and event names follow the Codex CLI's
documented `exec --json` mode but were not verified against an installed build
(no `codex` on this machine when written). Run `codex exec --help` to confirm.

No tools: the CLI runs its own sandboxed loop, so HunterAI's registry and permission engine
cannot gate what it does. Chats on Codex get the no-tools notice.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile

from .base import (ModelInfo, Options, ProviderError, Stop, TextDelta, ThinkingDelta, Usage,
                   WireMessage, flatten_text)

EXECUTABLE = "codex"
_EFFORTS = {"low", "medium", "high", "xhigh"}


def flatten_conversation(system: str, messages: list[WireMessage]) -> str:
    parts = [system.strip()] if system.strip() else []
    for m in messages:
        text = flatten_text(m)
        if text:
            parts.append(("Assistant:" if m.role == "assistant" else "User:") + "\n" + text)
    return "\n\n".join(parts)


class CodexProvider:
    name = "codex"

    def configured(self) -> bool:
        return shutil.which(EXECUTABLE) is not None

    async def models(self) -> list[ModelInfo]:
        return [ModelInfo(id="codex-default", label="Codex (CLI default model)",
                          options=["effort"])]

    async def stream(self, model: str, system: str, messages: list[WireMessage],
                     options: Options):
        exe = shutil.which(EXECUTABLE)
        if not exe:
            raise ProviderError("unavailable", "codex CLI is not installed or not on PATH")
        args = ["exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check"]
        if options.effort in _EFFORTS:
            args += ["-c", f'model_reasoning_effort="{options.effort}"']
        args.append("-")
        workdir = tempfile.mkdtemp(prefix="hunterai-codex-")
        proc = None
        usage, emitted, failure = Usage(0, 0), False, None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *args, cwd=workdir, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024, start_new_session=True)
            proc.stdin.write(flatten_conversation(system, messages).encode())
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
                item = ev.get("item") or {}
                if kind == "item.completed" and item.get("text"):
                    if item.get("type") == "agent_message":
                        emitted = True
                        yield TextDelta(item["text"])
                    elif item.get("type") == "reasoning":
                        yield ThinkingDelta(item["text"])
                elif kind == "turn.completed":
                    u = ev.get("usage") or {}
                    usage = Usage(int(u.get("input_tokens") or 0),
                                  int(u.get("output_tokens") or 0))
                elif kind in ("turn.failed", "error"):
                    err = ev.get("error") or ev
                    failure = str(err.get("message") or err)[:300]
            stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
            code = await proc.wait()
            if failure or (code != 0 and not emitted):
                detail = failure or stderr[-300:] or f"exit code {code}"
                raise ProviderError("unavailable", f"codex failed: {detail}")
        finally:
            if proc is not None and proc.returncode is None:
                # Kill the whole process group so helpers the CLI spawned die with it.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            shutil.rmtree(workdir, ignore_errors=True)
        yield usage
        yield Stop("end")


def make() -> CodexProvider:
    return CodexProvider()
