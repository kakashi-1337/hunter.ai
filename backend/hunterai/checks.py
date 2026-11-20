"""The check of a work folder: a command the owner set for it (the project's tests), run by HunterAI in coding
mode before a run's first change and after the run. The answer then shows both outcomes beside the list of what
the run changed, so the owner sees whether the run made the check pass or fail without taking the model's word.

Setting a check is the owner's decision that this command may run in this folder without a question, files the
model has just written included. Only the owner sets it (through the API, which no tool can call), and it is
kept per folder, not per chat.

A folder that is as the last check saw it is not checked again: the save point of the folder says whether it is
(see savepoint.py), so the check before a run is usually the one from after the run before it. What the folder
ignores does not count for that, so a change in an ignored file alone does not make the check run again.

The check needs the save point: a folder that cannot have one is not checked. Its output is the owner's to
read and never goes to a model."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from . import hub, procs, savepoint, secrets
from .repo import runs, settings, targets
from .tools import resolve
from .tools.base import ToolError
from .types import ChangesBlock

KEY = "check:"              # + the folder: the settings key the command is kept under
MAX_COMMAND = 2000
SECONDS = 300.0             # one run of the check; after that it is stopped and counts as failed
OUTPUT = 4000               # characters of its output kept with the answer: the end of it
LEFT_OUT = "[the output before this is left out]\n"
NOT_BY_ITSELF = "No check followed this run: it was stopped, or it failed."
STOPPED = "The check after this run was stopped before it ended."
_runs: dict[str, dict] = {}                         # run -> {"folder", "command", "before"}
_last: dict[tuple[str, str], tuple[str, dict]] = {}     # (folder, command) -> (the save point it ran on, what it said)


class Refused(ValueError):
    """A command that cannot be the check of a folder; the message says why, for the owner."""


def _folder(folder) -> str:
    return str(Path(folder).resolve())


async def command(conn, folder) -> str:
    """The check of this folder, or "" when it has none."""
    found = await settings.get(conn, KEY + _folder(folder), "")
    return found if isinstance(found, str) else ""


async def set_command(conn, folder, text: str) -> str:
    """Keep `text` as the check of this folder ("" removes it). Returns what is kept."""
    text = text.strip()
    if len(text) > MAX_COMMAND:
        raise Refused(f"the command is too long: {len(text):,} characters, at most {MAX_COMMAND:,}")
    if "\n" in text or "\r" in text or "\0" in text:
        raise Refused("the check is one line: put more than one step into a script and name the script")
    if secrets.scrub(text) != text:
        raise Refused("the command contains a key HunterAI keeps, or the login of a proxy; the check runs without it")
    if any(resolve.is_own_api(h) for h in resolve.extract_hosts(text)):
        raise Refused("the command addresses HunterAI's own API, which nothing HunterAI runs by itself may call")
    await settings.put(conn, KEY + _folder(folder), text)
    return text


async def _run(conn, run_id: str, chat, folder: str, text: str, when: str, parent_step_id: str | None = None) -> dict:
    """Run the check once, as a step of the run: {"passed", "exit_code", "seconds", "timed_out", "output"}.
    Stopping the run ends the check with it."""
    step = await runs.add_step(conn, run_id, "check", text[:300], {"when": when}, parent_step_id)
    status, said = "error", {"passed": False, "exit_code": None, "seconds": 0.0, "timed_out": False, "output": ""}
    began = time.monotonic()
    try:                                    # from here on the step is closed whatever happens, a stop included
        await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})
        from .tools import shell            # the same way out as a command of the chat: through its proxy, when it has one
        target = await targets.get(conn, chat.target_id) if chat.target_id else None
        _, env = await shell.way_out(target)
        proc = await procs.start(chat_id=chat.id, run_id=run_id, step_id=step.id, command=text, cwd=folder, background=False, env=env)
        done = await procs.wait(proc.id, SECONDS)
        said.update(seconds=round(time.monotonic() - began, 1), timed_out=done.status == "killed",
                    exit_code=done.exit_code if done.status == "exited" else None, output=_end_of(done.log_path))
        said["passed"] = done.status == "exited" and done.exit_code == 0
        status = "done"
    except ToolError as e:                  # it could not be started: no room for its output, no way to the proxy
        said["output"] = secrets.scrub(str(e))
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        detail = {k: said[k] for k in ("passed", "exit_code", "seconds", "timed_out")}     # added to "when", which the step has
        closed = await asyncio.shield(runs.patch_step(conn, step.id, status=status, detail=detail, close=True))
        await asyncio.shield(hub.publish(run_id, {"type": "step_finished", "step": closed.model_dump()}))
    return said


def _end_of(log_path: str) -> str:
    """The end of what the check wrote, without anything HunterAI keeps secret."""
    _, _, total = procs.read(log_path, 0, 0)
    room = OUTPUT * 4                       # bytes: enough for OUTPUT characters whatever they are
    text, _, _ = procs.read(log_path, max(0, total - room), room)
    if total > room:
        text = text.split("\n", 1)[-1]      # the line the cut went through: half a key would not be recognised as one
    text = secrets.scrub(text)
    return text if total <= room and len(text) <= OUTPUT else LEFT_OUT + text[-OUTPUT:]


async def before(conn, run_id: str, chat, parent_step_id: str | None = None) -> None:
    """Called right after the save point of a run was taken, before its first change: the check of the folder as
    it is now, unless the last check saw this very folder."""
    if run_id in _runs:
        return
    kept = savepoint.of_run(run_id)
    state = _runs[run_id] = {"folder": "", "command": "", "before": None}
    if kept is None or not kept[1]:
        return                              # no save point: nothing to hold the outcome against
    folder, tree = str(kept[0]), kept[1]
    text = await command(conn, folder)
    if not text:
        return
    state.update(folder=folder, command=text)
    seen = _last.get((folder, text))
    if seen and seen[0] == tree:
        state["before"] = seen[1]
        return
    try:
        state["before"] = await _run(conn, run_id, chat, folder, text, "before", parent_step_id)
    except BaseException:
        _runs.pop(run_id, None)
        raise
    # What the check itself wrote is not the run's: the run starts from the folder as the check left it.
    _last[(folder, text)] = (await savepoint.again(run_id) or tree, state["before"])


async def after(conn, run_id: str, chat, block: ChangesBlock | None) -> ChangesBlock | None:
    """Called when a run ended by itself, with the list of what it changed: the check of the folder as the run
    left it, added to the list. A run that changed nothing is not followed by one."""
    state = _runs.pop(run_id, None)
    if not state or not state["command"] or block is None or not block.files:
        return block
    folder, text = state["folder"], state["command"]
    seen = _last.get((folder, text))
    try:
        if seen and seen[0] == block.after:
            said = seen[1]
        else:
            said = await _run(conn, run_id, chat, folder, text, "after")
            _last[(folder, text)] = (await savepoint.take(folder) or block.after, said)
    except asyncio.CancelledError:
        return block.model_copy(update={"check": {"command": text, "before": state["before"], "after": None, "note": STOPPED}})
    return block.model_copy(update={"check": {"command": text, "before": state["before"], "after": said, "note": ""}})


def unfinished(run_id: str, block: ChangesBlock | None) -> ChangesBlock | None:
    """For a run that did not end by itself (failed, stopped): its list says that no check followed it."""
    state = _runs.pop(run_id, None)
    if not state or not state["command"] or block is None or not block.files or block.check is not None:
        return block
    return block.model_copy(update={"check": {"command": state["command"], "before": state["before"], "after": None,
                                              "note": NOT_BY_ITSELF}})
