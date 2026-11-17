"""The execute tools: run a command on the host shell, in the foreground or as a background
process whose output is read by offset."""
from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from pathlib import Path

from .. import config, procs
from . import resolve
from .base import PathUse, Prepared, Tool, ToolContext, ToolError, ToolResult


async def _prepare_execute(ctx: ToolContext, args: dict) -> Prepared:
    cwd = resolve.resolve_path(args.get("cwd") or ".", ctx.workdir)
    if not cwd.is_dir():
        raise ToolError(f"cwd is not a directory: {cwd}")
    command = args["command"]
    env_file = Path(resolve._real(config.env_file()))   # noqa: SLF001
    paths: dict[PathUse, None] = {}
    for token in re.split(r"[\s'\"<>|;&()=`$]+", command):
        if not token:
            continue
        try:
            if resolve.resolve_path(token, cwd) == env_file:
                paths[PathUse(str(env_file), "read")] = None   # denied as protected
        except ToolError:
            pass
    return Prepared(paths=tuple(paths), hosts=tuple(resolve.extract_hosts(command)),
                    cwd=str(cwd), grant_target=f"{cwd}\n{command}")


def _status(p: procs.Proc) -> str:
    if p.status == "exited":
        return f"status: exited (code {p.exit_code})"
    return f"status: {p.status}"


def _finished(p: procs.Proc, seconds: float, timeout: float | None) -> ToolResult:
    text, nxt, total = procs.read(p.log_path, 0, procs.FOREGROUND_BYTES)
    if p.status == "killed" and timeout is not None:
        head = f"timed out after {timeout:g}s: the process group was killed"
    elif p.status == "killed":
        head = "killed: the process group was ended"
    else:
        head = f"exit code: {p.exit_code}"
    out = f"{head}\nduration: {seconds:.1f}s\ncwd: {p.cwd}\n\n{text}"
    if nxt < total:
        out += f"\n[output continues: {p.log_path}]"
    return ToolResult(out, is_error=p.status == "killed",
                      meta={"exit_code": p.exit_code, "process_id": p.id,
                            "full_output_path": p.log_path})


async def _way_out(ctx: ToolContext):
    """(the proxy in effect for this chat or None, the variables that send a command's traffic to it).
    The variables name a hop on loopback, which holds the login: a command, and the model that wrote it,
    never see it."""
    from .. import hop, upstream
    up = await upstream.load(ctx.target.id if ctx.target is not None else None)
    if up is None:
        return None, {}
    try:
        return up, upstream.variables(await hop.address(up))
    except OSError as e:
        raise ToolError(f"the local hop to the proxy at {up.address} could not start ({type(e).__name__}). The command "
                        "was not run: without the hop its traffic would have gone out directly.") from None


def _not_carried(up, before: int) -> str:
    """A line for the end of a command's output when the proxy did not carry what the command asked for."""
    from .. import hop
    count, why = hop.refused(up)
    if count <= before:
        return ""
    n = count - before
    return (f"\n[the proxy at {up.address} did not carry {n} connection{'' if n == 1 else 's'} while this command "
            f"ran: {why}. Nothing was sent directly.]")


async def _execute(ctx: ToolContext, args: dict) -> ToolResult:
    cwd = ctx.prepared.cwd
    background = bool(args.get("background"))
    if background and len(procs.running(ctx.chat_id)) >= procs.MAX_PROCS_PER_CHAT:
        raise ToolError(f"this chat already has {procs.MAX_PROCS_PER_CHAT} background processes "
                        "running; kill one first")
    up, env = await _way_out(ctx)
    if up is not None:
        from .. import hop
        before = hop.refused(up)[0]
    began = time.monotonic()
    p = await procs.start(chat_id=ctx.chat_id, run_id=ctx.run_id, step_id=ctx.step_id,
                          command=args["command"], cwd=cwd, background=background, env=env)
    if not background:
        timeout = float(args.get("timeout") or procs.DEFAULT_TIMEOUT)
        done = await procs.wait(p.id, timeout)
        took = time.monotonic() - began
        result = _finished(done, took, timeout if done.status == "killed" and took >= timeout else None)
        note = _not_carried(up, before) if up is not None else ""
        return dataclasses.replace(result, text=result.text + note) if note else result
    deadline = time.monotonic() + procs.BACKGROUND_FIRST_WAIT
    while time.monotonic() < deadline and p.id in [x.id for x in procs.running(ctx.chat_id)]:
        await asyncio.sleep(0.05)
    now = await procs.get(p.id)
    if now.status != "running":
        return _finished(now, time.monotonic() - began, None)
    text, nxt, _ = procs.read(p.log_path)
    return ToolResult(
        f"started background process {p.id} (pid {p.pid})\n{text}\n"
        f"read more: process_output(id=\"{p.id}\", offset={nxt})",
        meta={"process_id": p.id, "keep_step_open": True})


async def _own(ctx: ToolContext, proc_id: str) -> procs.Proc:
    p = await procs.get(proc_id)
    if p is None or p.chat_id != ctx.chat_id:
        raise ToolError("no such process")
    return p


async def _output(ctx: ToolContext, args: dict) -> ToolResult:
    p = await _own(ctx, args["id"])
    offset = int(args.get("offset") or 0)
    deadline = time.monotonic() + int(args.get("wait_seconds") or 0)
    while True:
        p = await _own(ctx, args["id"])
        text, nxt, total = procs.read(p.log_path, offset)
        if total > offset or p.status != "running" or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.1)
    lines = [_status(p), f"bytes {offset}-{nxt} of {total}", text]
    if nxt < total or p.status == "running":
        lines.append(f"more: process_output(id=\"{p.id}\", offset={nxt})")
    return ToolResult("\n".join(lines))


async def _prepare_input(ctx: ToolContext, args: dict) -> Prepared:
    p = await _own(ctx, args["id"])
    return Prepared(cwd=p.cwd)   # no grant target: every input is a new command


async def _input(ctx: ToolContext, args: dict) -> ToolResult:
    p = await _own(ctx, args["id"])
    await procs.send_input(p.id, args["text"], bool(args.get("eof")))
    return ToolResult(f"sent {len(args['text'])} characters to process {p.id}")


async def _kill(ctx: ToolContext, args: dict) -> ToolResult:
    p = await _own(ctx, args["id"])
    if p.status == "running" and p.id in [x.id for x in procs.running(ctx.chat_id)]:
        p = await procs.kill(p.id)
    return ToolResult(_status(p))


_ID = {"type": "string"}


def tools() -> list[Tool]:
    return [
        Tool("execute", (
            "Run a shell command on this machine (bash -c). Foreground commands return their "
            "exit code and output; set background=true for scans, listeners and anything long, "
            "then read it with process_output."),
            {"type": "object", "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 20000},
                "cwd": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": procs.MAX_TIMEOUT},
                "background": {"type": "boolean"}},
             "required": ["command"], "additionalProperties": False},
            "execute", _execute, prepare=_prepare_execute),
        Tool("process_output", "Read the output of a process this chat started, from an offset.",
             {"type": "object", "properties": {
                 "id": _ID, "offset": {"type": "integer", "minimum": 0, "default": 0},
                 "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 30, "default": 0}},
              "required": ["id"], "additionalProperties": False},
             "read", _output),
        Tool("process_input", (
            "Send text to the standard input of a background process this chat started. "
            "Include the newline yourself."),
            {"type": "object", "properties": {"id": _ID, "text": {"type": "string"},
                                              "eof": {"type": "boolean", "default": False}},
             "required": ["id", "text"], "additionalProperties": False},
            "execute", _input, prepare=_prepare_input),
        Tool("process_kill", "End a process this chat started (its whole process group).",
             {"type": "object", "properties": {"id": _ID}, "required": ["id"],
              "additionalProperties": False},
             "read", _kill),
    ]
