"""Runs one model turn for a chat: builds context, streams, falls back, records the trajectory.

A run lives in its own asyncio task with its own database connection, so it keeps
going when the browser disconnects. Progress goes to the hub for live viewers and
to SQLite so it survives a restart.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from typing import Awaitable, Callable

from . import checks, context, cooling, hub, permissions, pictures, problems, savepoint, secrets, uploads
from .modes import build_system
from .providers import registry
from .providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                             ThinkingSeal, ToolCall, ToolSpec, Usage, WireMessage, safe_call_id)
from .repo import chats, goals, messages, runs, settings, targets
from .tools import loopguard, runner
from .types import (NOT_FOR_MODELS, Block, ChangesBlock, ImageBlock, NoticeBlock, OwnerNoteBlock, TextBlock, ThinkingBlock,
                    ToolCallBlock, ToolResultBlock)

DEFAULT_PROVIDER = "abliteration"
FALLBACK_PROVIDER = "ollama"
DEFAULT_FALLBACK_MODEL = "qwen3:8b"
FLUSH_SECONDS = 0.5
MAX_RATE_LIMIT_WAIT = 20
RETRY_PAUSE = 3                # seconds before a later model call that reached nobody is made once more
DEFAULT_MAX_TURNS = 25      # model calls per run (setting "max_turns")
MAX_TURNS_LIMIT = 200
MAX_BAD_CALLS = 3           # invalid tool calls in a row
MAX_CALLS_PER_TURN = 20
CIRCLE_TAIL = 120           # characters at the end of what a model wrote that are looked for earlier in it
CIRCLE_TIMES = 3            # found that often, the model goes round in a circle
INTERRUPTED_ASK = "The app stopped while this run was waiting for a permission answer."

_sleep = asyncio.sleep
_tasks: dict[str, asyncio.Task] = {}
# A task cancelled before its first step never runs its body, so nothing would record the
# outcome. Cancellation is therefore a flag the task checks once it starts, plus a real
# task.cancel() only after it has started.
_started: set[str] = set()
_cancel_requested: set[str] = set()
_finalizing: set[str] = set()  # past the point where cancelling means anything

LoadBytes = Callable[[str], Awaitable[bytes]]


class _Attempt:
    """The blocks of one run in arrival order, shared by all its model calls."""

    def __init__(self):
        self.items: list[Block] = []
        self.last_flush = 0.0
        self.output = False        # the model call in progress has produced something
        self.calls: list[ToolCall] = []   # tool calls of the model call in progress
        self.taken: set[str] = set()      # tool-call ids already used in this conversation
        self.sources = None               # what the knowledge base gave this run, if anything
        self.check = None                 # the answer check of this run, once it is made
        self.callable: list[str] = []     # every tool this run may call: the listed ones and the ones loaded on demand
        self.stop: str | None = None      # why the last model call ended, in the provider's word
        self.produced = 0                 # output tokens of the last model call
        self.notice: NoticeBlock | None = None    # what the owner has to know about how the answer ended
        self.window: int | None = None    # the context window this run raised a local model's to: its later calls get it too
        self.window_was: int | None = None        # the window before the run raised it the first time
        self.grew: NoticeBlock | None = None      # that the window was raised, and from what, for the owner
        self.stuck: NoticeBlock | None = None     # that the window could not be raised: the model did not answer with it
        self.circles = False              # the last model call filled its window by saying one thing over and over
        self.goal: NoticeBlock | None = None      # the answer says complete while items of the goal record are open
        self.changes: ChangesBlock | None = None  # what the run changed in the work folder (coding mode)

    @property
    def started(self) -> bool:
        return bool(self.items)

    def blocks(self) -> list[Block]:
        """What is stored: the run's blocks, then the word about how the answer ended and about the open items of
        the goal record, what the run changed in the work folder, the knowledge-base sources and the answer check,
        when there are any."""
        return [*self.items, *(b for b in (self.grew, self.stuck, self.notice, self.goal, self.changes, self.sources,
                                           self.check) if b is not None)]

    def wire_blocks(self) -> list[Block]:
        """What goes back to the model: never a block that is only the owner's to read."""
        return [b for b in self.items if b.type not in NOT_FOR_MODELS]

    def text(self, delta: str) -> None:
        last = self.items[-1] if self.items else None
        if last is not None and last.type == "text":
            last.text += delta
        else:
            self.items.append(TextBlock(text=delta))

    def thinking(self, delta: str) -> None:
        last = self.items[-1] if self.items else None
        if last is not None and last.type == "thinking" and last.opaque is None:
            last.text += delta
        else:
            self.items.append(ThinkingBlock(text=delta))

    def seal(self, opaque: dict) -> None:
        last = self.items[-1] if self.items else None
        if last is not None and last.type == "thinking" and last.opaque is None:
            last.opaque = opaque
        else:
            self.items.append(ThinkingBlock(text="", opaque=opaque))


async def start_run(conn_factory, chat_id: str, user_message_id: str,
                    load_bytes: LoadBytes) -> tuple[str, str]:
    conn = await conn_factory()
    try:
        chat = await chats.get(conn, chat_id)
        assistant = await messages.add(conn, chat_id, user_message_id, "assistant", [],
                                       provider=chat.provider, model=chat.model,
                                       status="streaming")
        run = await runs.create(conn, chat_id, user_message_id, assistant.id)
    finally:
        await conn.close()
    hub.open(run.id)
    await hub.publish(run.id, {"type": "run_started", "run_id": run.id,
                               "assistant_message_id": assistant.id})
    task = asyncio.create_task(_execute(conn_factory, run.id, chat_id, user_message_id,
                                        assistant.id, load_bytes))
    _tasks[run.id] = task
    task.add_done_callback(lambda _t, rid=run.id: _forget(rid))
    return run.id, assistant.id


def _forget(run_id: str) -> None:
    _tasks.pop(run_id, None)
    _started.discard(run_id)
    _cancel_requested.discard(run_id)
    _finalizing.discard(run_id)


async def wait(run_id: str) -> None:
    """Wait for a run's task to finish (no-op if it already has)."""
    task = _tasks.get(run_id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def is_active(run_id: str) -> bool:
    """True while this process is still executing the run."""
    task = _tasks.get(run_id)
    return task is not None and not task.done()


async def cancel(run_id: str) -> bool:
    task = _tasks.get(run_id)
    if task is None or task.done() or run_id in _finalizing:
        return False
    _cancel_requested.add(run_id)
    if run_id in _started:
        task.cancel()
    return True


async def shutdown() -> None:
    """Stop every in-flight run and wait until each has written its final state.

    A run waiting for a permission answer ends "interrupted" (never approved); cancelling
    its task as well would replace that with a plain cancel."""
    tasks = [t for t in _tasks.values() if not t.done()]
    parked = permissions.interrupt_all()
    for run_id in list(_tasks):
        if run_id not in parked:
            await cancel(run_id)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def recover(conn) -> None:
    """After a restart: nothing is running any more, so close out what was."""
    await runs.mark_interrupted(conn)
    await conn.execute("update messages set status='interrupted' where status='streaming'")
    await conn.commit()


async def _picture(conn, b: ImageBlock, images: dict[str, bytes], load_bytes: LoadBytes) -> Block:
    """A picture of a message as a model gets it: its bytes go into `images`, in a kind every reader takes, and
    the block is returned under the name of that kind. One that cannot be decoded is not sent, because the
    program that runs a local model refuses the whole request for it, and so every later turn of the chat
    (pictures.py): the block returned for it says in words that it was left out, and why."""
    try:
        data = await load_bytes(b.upload_id)
    except Exception:
        return b  # adapter renders a missing image as a placeholder
    try:
        seen = await pictures.for_model(data)
    except pictures.Unreadable as e:
        upload = await uploads.get(conn, b.upload_id)
        return TextBlock(text=pictures.omitted(upload.filename if upload else None, str(e)))
    images[b.upload_id] = seen.data
    return b if b.mime == seen.mime else b.model_copy(update={"mime": seen.mime})


async def _context(conn, chat, user_message_id: str, load_bytes: LoadBytes) -> list[WireMessage]:
    out: list[WireMessage] = []
    for m in await messages.path(conn, chat.id, user_message_id):
        has_text = any((b.type == "text" and b.text) or b.type == "tool_call" for b in m.blocks)
        if m.role == "assistant" and not has_text:
            continue  # failed or empty turns carry nothing the model can use
        images: dict[str, bytes] = {}
        blocks = [await _picture(conn, b, images, load_bytes) if b.type == "image" else b
                  for b in m.blocks if b.type not in NOT_FOR_MODELS]
        out.append(WireMessage(role=m.role, blocks=blocks, images=images, id=m.id))
    return out


async def _fitted(conn, run_id: str, provider, provider_name: str, model: str, system: str,
                  wire: list[WireMessage], options: Options, specs: list[ToolSpec] | None, note: dict):
    """The provider's events for one call, with the request made to fit the model's context
    window first (see context.py). When the provider refuses the request for its size before it
    has sent anything, the request is made smaller once more and the call is made again, once.
    `note` gets what was done to the request, for the trajectory."""
    async def fit(hard: bool = False) -> list[WireMessage]:
        try:
            fitted, done = await context.fit(conn, run_id, provider_name, model, system, wire, options, specs, hard=hard)
        except Exception as e:      # a fault in the shortening must not break a chat that would have worked
            fitted, done = wire, {"error": f"{type(e).__name__}: {e}"[:300]}
        if done:
            note["context"] = done
        return fitted

    def events(fitted: list[WireMessage]):
        if specs and getattr(provider, "supports_tools", False):
            return provider.stream(model, system, fitted, options, tools=specs)
        return provider.stream(model, system, fitted, options)   # the Phase 1 call

    fitted, sent = await fit(), False
    try:
        async with contextlib.aclosing(events(fitted)) as stream:
            async for ev in stream:
                sent = True
                yield ev
        return
    except ProviderError as e:
        if sent or not context.is_overflow(e):
            raise
        refused = e
    smaller = await fit(hard=True)
    if smaller is fitted or smaller is wire:
        raise ProviderError(refused.kind, refused.message + context.TOO_LONG, refused.status, complete=True)
    try:
        async with contextlib.aclosing(events(smaller)) as stream:
            async for ev in stream:
                yield ev
    except ProviderError as e:
        if context.is_overflow(e):
            raise ProviderError(e.kind, e.message + context.TOO_LONG, e.status, complete=True) from None
        raise


async def _attempt(conn, run_id: str, mid: str, provider_name: str, model: str, system: str,
                   wire: list[WireMessage], options: Options, acc: _Attempt,
                   specs: list[ToolSpec] | None = None) -> None:
    """One model call. Raises ProviderError; always closes its trajectory step. A model that has to be loaded
    onto this machine is called once the machine is cool enough for that (cooling.py): _Stopped when it does not cool.

    Tool calls the model makes are left in acc.calls for the caller to run."""
    try:
        await cooling.before_call(conn, run_id, provider_name, model)
    except cooling.TooHot as e:
        raise _Stopped(str(e)) from None
    step = await runs.add_step(conn, run_id, "model_call", f"{provider_name}/{model}", {})
    await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})
    usage = Usage(0, 0)
    status, detail, fit = "error", {}, {}
    acc.output, acc.calls, acc.stop, acc.produced = False, [], None, 0
    try:
        provider = registry.get(provider_name)
        async for ev in _fitted(conn, run_id, provider, provider_name, model, system, wire, options, specs, fit):
            flush = False
            if isinstance(ev, TextDelta):
                acc.text(ev.text)
                acc.output = True
                await hub.publish(run_id, {"type": "text_delta", "text": ev.text})
            elif isinstance(ev, ThinkingDelta):
                acc.thinking(ev.text)
                acc.output = True
                await hub.publish(run_id, {"type": "thinking_delta", "text": ev.text})
            elif isinstance(ev, ThinkingSeal):
                acc.seal(ev.opaque)
            elif isinstance(ev, ToolCall):
                # The id is a key in events, steps, and the UI: make it safe and unique.
                tc = dataclasses.replace(ev, id=safe_call_id(ev.id, acc.taken))
                acc.taken.add(tc.id)
                acc.calls.append(tc)   # the runner gets the model's own arguments
                acc.output = True
                shown = secrets.scrub_obj(tc.arguments)   # what is stored and published
                error = secrets.scrub(tc.error) if tc.error else None
                acc.items.append(ToolCallBlock(id=tc.id, name=tc.name, arguments=shown,
                                               opaque=tc.opaque))
                await hub.publish(run_id, {"type": "tool_call", "id": tc.id, "name": tc.name,
                                           "arguments": shown, "error": error})
                flush = True
            elif isinstance(ev, Usage):
                usage = ev
                acc.produced = ev.output_tokens
            elif isinstance(ev, Stop):
                detail = {"stop_reason": ev.reason}
                acc.stop = ev.reason
            if acc.started and (flush or time.monotonic() - acc.last_flush >= FLUSH_SECONDS):
                await messages.update(conn, mid, blocks=acc.blocks())
                acc.last_flush = time.monotonic()
        status = "done"
    except ProviderError as e:
        detail = {"error": e.message, "kind": e.kind}
        e.provider, e.model = provider_name, model      # which call it was: the fallback's error is not the first model's
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except KeyError as e:
        detail = {"error": f"unknown provider {provider_name}"}
        raise ProviderError("bad_request", f"This chat is set to the provider \"{provider_name}\", which this HunterAI does "
                            "not have. Pick a model from the list for this chat.", complete=True) from e
    except Exception as e:  # an adapter bug must not leave the run hanging
        detail = {"error": f"{type(e).__name__}: {e}"}
        raise ProviderError("bad_request", problems.own_failure(e, f"calling {provider_name}/{model}"), complete=True) from e
    finally:
        done = await asyncio.shield(runs.finish_step(
            conn, step.id, status, usage.input_tokens, usage.output_tokens, {**detail, **fit}))
        await asyncio.shield(hub.publish(run_id, {"type": "step_finished",
                                                  "step": done.model_dump()}))


async def _mark(conn, run_id: str, kind: str, name: str, detail: dict) -> None:
    """A step of the run for something that takes no time: it is there, and done."""
    step = await runs.add_step(conn, run_id, kind, name, detail)
    await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})
    step = await runs.patch_step(conn, step.id, status="done", close=True)
    await hub.publish(run_id, {"type": "step_finished", "step": step.model_dump()})


async def _raise_window(conn, run_id: str, provider_name: str, model: str, options: Options, acc: _Attempt,
                        to: int, why: str):
    """Give this call and the later ones of the run a context window of `to` tokens: a step of the run, a word under
    the answer, and the chat's own window from now on when the model is the chat's own (one that answers in place of
    the chat's model gets it for this run only: the size is not the other model's). Returns the options to call
    with, and what takes all of it back when the model does not answer with that window.

    A window of another size is another load of the model, on a machine that may just have been answering: the
    machine is given the time to cool that any load gets (cooling.py), before anything is changed. _Stopped when it
    does not cool."""
    try:
        await cooling.before_call(conn, run_id, provider_name, model, reload=True)
    except cooling.TooHot as e:
        raise _Stopped(str(e)) from None
    before = await registry.get(provider_name).window_for(model, options)
    await _mark(conn, run_id, "context", f"context window {before:,} → {to:,}", {"window": {"from": before, "to": to, "why": why}})
    run = await runs.get(conn, run_id)
    chat = await chats.get(conn, run.chat_id) if run else None
    own = chat is not None and (chat.provider, chat.model) == (provider_name, model)
    if own:
        await chats.update(conn, chat.id, options={**(chat.options or {}), "context_window": to})
    held = (acc.window, acc.window_was, acc.grew)
    acc.window, acc.window_was = to, before if acc.window_was is None else acc.window_was
    acc.grew = NoticeBlock(kind="window", text=problems.window_grew(acc.window_was, to, again=why == "answer", kept=own))

    async def back(error: ProviderError) -> None:
        acc.window, acc.window_was, acc.grew = held
        now = await chats.get(conn, chat.id) if own else None
        if now is not None and (now.options or {}).get("context_window") == to:     # not what the owner set meanwhile
            was = (chat.options or {}).get("context_window")
            rest = {k: v for k, v in now.options.items() if k != "context_window"}
            await chats.update(conn, chat.id, options={**rest, "context_window": was} if was else rest)
        await _mark(conn, run_id, "context", f"context window {to:,} → {before:,}",
                    {"window": {"from": to, "to": before, "why": "failed", "error": error.message[:300]}})

    return options.model_copy(update={"context_window": to}), back


async def _stays(provider_name: str, model: str, options: Options, acc: _Attempt, tried: int, error: ProviderError) -> None:
    """The model answers with the window it had and did not with one of `tried` tokens: that size is not tried for
    it again (loading a model that does not fit takes its time, and the machine's), and the owner is told."""
    provider = registry.get(provider_name)
    refuse = getattr(provider, "too_large", None)
    if refuse is not None:
        refuse(model, tried)
    acc.stuck = NoticeBlock(kind="window", text=problems.window_stayed(await provider.window_for(model, options), tried,
                                                                       error.message))


async def _restart(run_id: str, acc: _Attempt, mark: int, blocks: list[Block]) -> None:
    """Put `blocks` in place of what the run wrote from `mark` on, and tell the page of whoever watches the run. The
    stored message follows with the next thing that is written: until then it keeps the answer that was cut off,
    which is what should be left of the run if HunterAI stops here."""
    acc.items[mark:] = blocks
    await hub.publish(run_id, {"type": "answer_restarted", "blocks": [b.model_dump() for b in acc.items]})


async def _call(conn, run_id: str, mid: str, provider_name: str, model: str, system: str,
                wire: list[WireMessage], options: Options, acc: _Attempt,
                specs: list[ToolSpec] | None = None) -> None:
    """One model call of the run (_attempt), with the context window of a local model made larger when the call
    needs it. A chat that does not fit the window gets the next size that holds it before anything of it is left
    out (context.roomier). An answer that filled the window is taken back and written again in the next size, once:
    not one that called a tool, and not one that says the same thing over and over. The window stays that size for
    the rest of the run.

    A model that does not answer with the larger window is called with the one it had: the chat is then shortened
    as before, and an answer that was cut off stays as it was."""
    if acc.window:
        options = options.model_copy(update={"context_window": acc.window})
    args = (conn, run_id, mid, provider_name, model, system, wire)
    mark, acc.circles = len(acc.items), False
    larger = await context.roomier(provider_name, model, system, wire, options, specs)
    if larger:
        raised, back = await _raise_window(conn, run_id, provider_name, model, options, acc, larger, "request")
        try:
            await _attempt(*args, raised, acc, specs)
            options = raised
        except ProviderError as e:
            if acc.output or context.is_overflow(e):
                raise
            await back(e)
            await _attempt(*args, options, acc, specs)
            await _stays(provider_name, model, options, acc, larger, e)
    else:
        await _attempt(*args, options, acc, specs)
    sizes = await _larger(provider_name, model, options) if _window_filled(options, acc) else []
    acc.circles = bool(sizes) and _circles(acc.items[mark:])
    if not sizes or acc.circles:
        return
    cut = (acc.items[mark:], acc.stop, acc.produced)
    raised, back = await _raise_window(conn, run_id, provider_name, model, options, acc, sizes[0], "answer")
    await _restart(run_id, acc, mark, [])
    try:
        await _attempt(*args, raised, acc, specs)
    except ProviderError as e:
        if acc.output:
            raise
        await back(e)
        await _restart(run_id, acc, mark, cut[0])
        acc.stop, acc.produced = cut[1:]
        await _stays(provider_name, model, options, acc, sizes[0], e)


def _window_filled(options: Options, acc: _Attempt) -> bool:
    """Whether the model call that just ended is one to make again with more room: it stopped at a limit, which was
    not the one the chat sets for an answer, and it called no tool (its calls are run, and the next call gets the
    room it needs)."""
    return (problems.at_limit(acc.stop) and not acc.calls
            and not (options.max_tokens and acc.produced >= options.max_tokens))


def _circles(blocks: list[Block]) -> bool:
    """Whether what a model wrote goes round in a circle: its last CIRCLE_TAIL characters stand in it CIRCLE_TIMES
    times or more. Its thinking counts as its answer does. Such a model fills any window. (What is shorter than
    that is its own end, and stands in itself once.)"""
    said = "".join(b.text for b in blocks if b.type in ("text", "thinking"))
    tail = said[-CIRCLE_TAIL:]
    found, at = 0, said.find(tail)
    while at != -1 and found < CIRCLE_TIMES:
        found, at = found + 1, said.find(tail, at + 1)
    return found >= CIRCLE_TIMES


async def _larger(provider_name: str, model: str, options: Options) -> list[int]:
    """The windows a call to this model could be given in place of the one it has, smallest first. None for a
    provider that does not set the window of a call itself, and none when it cannot say."""
    try:
        ask = getattr(registry.get(provider_name), "larger_windows", None)
        return await ask(model, options) if ask is not None else []
    except Exception:  # noqa: BLE001 - the window stays as it is
        return []


async def _generate(conn, run_id: str, mid: str, chat, system: str, wire: list[WireMessage],
                    acc: _Attempt, specs: list[ToolSpec] | None = None
                    ) -> tuple[str, str, str | None]:
    """The first model call of a run, with the retry and fallback rules.
    Returns (provider, model, fallback_from)."""
    options = Options(**(chat.options or {}))
    args = (conn, run_id, mid)
    try:
        await _call(*args, chat.provider, chat.model, system, wire, options, acc, specs)
        return chat.provider, chat.model, None
    except ProviderError as e:
        err = e
    if acc.started:
        raise err
    if err.kind == "rate_limit" and err.retry_after:
        await _sleep(min(err.retry_after, MAX_RATE_LIMIT_WAIT))
        try:
            await _call(*args, chat.provider, chat.model, system, wire, options, acc, specs)
            return chat.provider, chat.model, None
        except ProviderError as e:
            err = e
        if acc.started:
            raise err
    if not (err.fallback_eligible and chat.provider == DEFAULT_PROVIDER):
        raise err
    fb_model = await settings.get(conn, "fallback_model", DEFAULT_FALLBACK_MODEL)
    reason = f"{err.kind}: {err.message}"
    step = await runs.add_step(conn, run_id, "fallback",
                               f"{chat.provider} → {FALLBACK_PROVIDER}", {})
    step = await runs.finish_step(conn, step.id, "done", detail={
        "from": f"{chat.provider}/{chat.model}", "to": f"{FALLBACK_PROVIDER}/{fb_model}",
        "reason": reason})
    await hub.publish(run_id, {"type": "step_finished", "step": step.model_dump()})
    await hub.publish(run_id, {
        "type": "fallback", "from_provider": chat.provider, "from_model": chat.model,
        "to_provider": FALLBACK_PROVIDER, "to_model": fb_model, "reason": reason})
    await _call(*args, FALLBACK_PROVIDER, fb_model, system, wire, options, acc, specs)
    return FALLBACK_PROVIDER, fb_model, f"{chat.provider}/{chat.model}"


async def _next_turn(conn, run_id: str, mid: str, provider: str, model: str, system: str,
                     wire: list[WireMessage], options: Options, acc: _Attempt,
                     specs: list[ToolSpec]) -> None:
    """A model call after tools ran: same provider and model, never a fallback. When the call
    produced nothing it is made once more: after the wait a rate limit names, or after a short
    pause when the provider could not be reached or did not answer in time (the tools' work
    should not be lost to one failed connection)."""
    try:
        await _call(conn, run_id, mid, provider, model, system, wire, options, acc, specs)
    except ProviderError as e:
        if acc.output:
            raise
        if e.kind == "rate_limit" and e.retry_after:
            await _sleep(min(e.retry_after, MAX_RATE_LIMIT_WAIT))
        elif e.kind in ("timeout", "unavailable"):
            await _sleep(RETRY_PAUSE)
        else:
            raise
        await _call(conn, run_id, mid, provider, model, system, wire, options, acc, specs)


class _Stopped(Exception):
    """The loop hit one of its caps; the message is the run's error."""


async def _tool_loop(conn, run_id: str, mid: str, chat, provider: str, model: str, system: str,
                     wire: list[WireMessage], acc: _Attempt, specs: list[ToolSpec]) -> None:
    """Run the tool calls of each model turn, then call the model again, until it answers
    without tool calls or a cap is hit."""
    options = Options(**(chat.options or {}))
    names = acc.callable or [s.name for s in specs]
    try:
        max_turns = int(await settings.get(conn, "max_turns", DEFAULT_MAX_TURNS))
    except (TypeError, ValueError):
        max_turns = DEFAULT_MAX_TURNS
    max_turns = max(1, min(MAX_TURNS_LIMIT, max_turns))

    async def publish(event: dict) -> None:
        await hub.publish(run_id, event)

    async def record(block: ToolResultBlock) -> None:
        acc.items.append(block)
        await messages.update(conn, mid, blocks=acc.blocks())
        acc.last_flush = time.monotonic()

    turn, bad = 1, 0
    guard = loopguard.Guard()          # this run's memory of calls that keep failing
    while acc.calls:
        calls = list(acc.calls)
        if turn >= max_turns:
            for tc in calls:
                await record(ToolResultBlock(
                    tool_call_id=tc.id, is_error=True,
                    content=f"Not run: this run reached its limit of {max_turns} model turns."))
            raise _Stopped(f"Stopped after {max_turns} model turns (the max_turns setting). "
                           "Send a message to continue.")
        for n, tc in enumerate(calls):
            if n >= MAX_CALLS_PER_TURN:
                await record(ToolResultBlock(
                    tool_call_id=tc.id, is_error=True,
                    content=f"Not run: more than {MAX_CALLS_PER_TURN} tool calls in one turn. "
                            "Send the remaining calls in your next turn."))
                continue
            outcome = await runner.call(conn, chat_id=chat.id, run_id=run_id, tc=tc,
                                        offered=names, publish=publish, guard=guard)
            await record(outcome.block)
            bad = bad + 1 if outcome.invalid else 0
            if bad >= MAX_BAD_CALLS:
                raise _Stopped(f"The model sent {MAX_BAD_CALLS} invalid tool calls in a row.")
            stuck = guard.stuck()
            if stuck:       # it answers each refusal with the call that was refused: more turns would be more of the same
                for left in calls[n + 1:]:
                    await record(ToolResultBlock(tool_call_id=left.id, is_error=True, content="Not run: this run was stopped."))
                raise _Stopped(stuck)
        turn += 1
        await _hand_over_queued(conn, run_id, mid, chat.id, acc, publish)
        await _next_turn(conn, run_id, mid, provider, model, system,
                         wire + [WireMessage("assistant", acc.wire_blocks())], options, acc, specs)


async def _hand_over_queued(conn, run_id: str, mid: str, chat_id: str, acc: _Attempt, publish) -> None:
    """What the owner sent while the agent worked goes to the model now, at the end of the last
    tool result, inside the block the system prompt announced. Nothing was interrupted for it."""
    from .repo import queue
    waiting = await queue.take(conn, chat_id)
    last = next((i for i in range(len(acc.items) - 1, -1, -1) if acc.items[i].type == "tool_result"), None)
    if not waiting or last is None:
        return
    texts = [q.text for q in waiting]
    old = acc.items[last]
    acc.items[last] = ToolResultBlock(tool_call_id=old.tool_call_id, is_error=old.is_error,
                                      content=old.content + queue.block(await queue.nonce(conn, chat_id), texts))
    acc.items.append(OwnerNoteBlock(text="\n\n".join(texts)))       # where it was read, for the transcript
    await messages.update(conn, mid, blocks=acc.blocks())
    step = await runs.add_step(conn, run_id, "user_note", "message from you", {"text": "\n\n".join(texts)[:4000], "count": len(texts)})
    await publish({"type": "step_started", "step": step.model_dump()})
    step = await runs.patch_step(conn, step.id, status="done", close=True)
    await publish({"type": "step_finished", "step": step.model_dump()})


def _title_from(text: str) -> str:
    return " ".join(text.split())[:60].strip()


async def _open(conn_factory):
    """Open a connection without leaking it if this task is cancelled meanwhile.

    Returns (conn, cancelled). The connect always runs to completion, because an
    abandoned half-open connection would keep its worker thread alive.
    """
    opening = asyncio.ensure_future(conn_factory())
    try:
        return await asyncio.shield(opening), False
    except asyncio.CancelledError:
        return await opening, True


async def _execute(conn_factory, run_id: str, chat_id: str, user_message_id: str, mid: str,
                   load_bytes: LoadBytes) -> None:
    acc = _Attempt()
    status, error = "error", None
    provider = model = fallback_from = None
    _started.add(run_id)
    conn = None
    try:
        conn, cancelled = await _open(conn_factory)
        if cancelled or run_id in _cancel_requested:
            raise asyncio.CancelledError
        chat = await chats.get(conn, chat_id)
        target = await targets.get(conn, chat.target_id) if chat.target_id else None
        # The tool list and the system prompt are fixed for the whole run; the permission
        # policy is re-read by the runner on every call.
        specs: list[ToolSpec] = []
        try:
            tool_capable = getattr(registry.get(chat.provider), "supports_tools", False)
        except KeyError:
            tool_capable = False
        later = []                # tools the model reaches through mcp_find and mcp_call, not in its list
        if tool_capable:
            shown, later = await runner.for_model(conn, await runner.offered_tools(conn, chat))
            specs = [ToolSpec(t.name, t.description, t.parameters) for t in shown]
            acc.callable = [t.name for t in (*shown, *later)]
        from .repo import personas
        persona = await personas.get(conn, chat.persona_id)
        if specs:
            system = build_system(chat.mode, target, [s.name for s in specs],
                                  chats.resolve_workdir(chat), goal=await goals.get(conn, chat_id),
                                  persona=persona)
            from . import upstream
            via = await upstream.told(conn, chat.target_id, [s.name for s in specs])
            if via:
                system += "\n\n" + via
            if later:
                from .tools import on_demand
                system += "\n\n" + on_demand.index(later)
            from .repo import queue
            system += "\n\n" + queue.rule(await queue.nonce(conn, chat_id))
        else:
            system = build_system(chat.mode, target, persona=persona)
        wire = await _context(conn, chat, user_message_id, load_bytes)
        acc.taken = {b.id for m in wire for b in m.blocks if b.type == "tool_call"}
        from . import skills
        about_skills = await skills.system_block(conn, wire, [s.name for s in specs])
        if about_skills:
            system += "\n\n" + about_skills
        provider, model = chat.provider, chat.model
        # Knowledge-base reference goes into the outgoing request only: `system` and `wire`
        # keep their values (the chat title below is taken from the user's own words).
        from .kb import retrieve
        from . import memory
        from .providers.base import flatten_text
        system_out, wire_out, acc.sources = await retrieve.augment(conn, run_id, chat, system, wire)
        # Notes from memory are searched with the user's own words, not with the reference block.
        system_out, wire_out = await memory.recall(conn, run_id, chat, system_out, wire_out,
                                                   query=flatten_text(wire[-1]) if wire else "")
        provider, model, fallback_from = await _generate(conn, run_id, mid, chat, system_out,
                                                         wire_out, acc, specs)
        await _tool_loop(conn, run_id, mid, chat, provider, model, system_out, wire_out, acc, specs)
        status = "done"
        acc.notice = await _ending(chat, provider, model, acc)
        if specs:                 # a chat with tools: its answer can claim things the tool results do not show
            await _check_answer(conn, run_id, acc)
            await _check_goal(conn, run_id, chat_id, acc)
        if chat.title == "New chat":
            first = next((b.text for m in wire if m.role == "user" for b in m.blocks
                          if b.type == "text" and b.text), "")
            first = await skills.title_text(conn, first)        # "/plan map the login" is titled by the task
            if _title_from(first):
                await chats.update(conn, chat_id, title=_title_from(first))
        await _folder_check(conn, run_id, chat, acc)        # last: stopping it stops nothing else of the run
    except ProviderError as e:
        error = problems.model_call(e)
    except _Stopped as e:
        error = str(e)
    except permissions.Interrupted:
        status, error = "interrupted", INTERRUPTED_ASK
    except asyncio.CancelledError:
        status = "cancelled"
    except Exception as e:
        error = problems.own_failure(e, "working on this message")
    finally:
        _finalizing.add(run_id)
        await asyncio.shield(_finalize(conn_factory, conn, run_id, mid, acc, status, error,
                                       provider, model, fallback_from))


async def _ending(chat, provider_name: str, model: str, acc: _Attempt) -> NoticeBlock | None:
    """The word for the owner when the last model call did not end by itself: cut off at a limit, or ended by the
    provider's filter. The answer looks finished otherwise, and stops in the middle of a sentence."""
    try:
        options = Options(**(chat.options or {}))
        if acc.window:              # the run raised it: that is the window the answer was written in
            options = options.model_copy(update={"context_window": acc.window})
        window = None
        window_for = getattr(registry.get(provider_name), "window_for", None)
        if window_for is not None and problems.at_limit(acc.stop):
            window = await window_for(model, options)
        said = problems.cut_off(acc.stop, provider=provider_name, max_tokens=options.max_tokens,
                                output_tokens=acc.produced, window=window, circles=acc.circles)
        return NoticeBlock(kind=said[0], text=said[1]) if said else None
    except Exception:  # noqa: BLE001 - the run is done; a word that cannot be made is left out
        return None


async def _check_answer(conn, run_id: str, acc: _Attempt) -> None:
    """Hold the final answer against the run's tool results. Whatever goes wrong here, the
    run is done and stays done: the check is an extra, recorded as its own step."""
    from . import answer_check
    step = None
    try:
        if not await answer_check.wanted(conn):
            return
        if len(answer_check.material(acc.items)[0]) < answer_check.MIN_ANSWER:
            return
        step = await runs.add_step(conn, run_id, "decision", "answer check", {})
        await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})
        status, detail = "error", {}
        try:
            acc.check, detail = await answer_check.judge(conn, acc.items)
            status = "error" if detail.get("error") else "done"
        except Exception as e:  # noqa: BLE001
            detail = {"error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            done = await asyncio.shield(runs.patch_step(conn, step.id, status=status, detail=detail or None, close=True))
            await asyncio.shield(hub.publish(run_id, {"type": "step_finished", "step": done.model_dump()}))
    except asyncio.CancelledError:
        pass                      # stopped while checking: the answer itself was complete
    except Exception:  # noqa: BLE001
        pass


async def _check_goal(conn, run_id: str, chat_id: str, acc: _Attempt) -> None:
    """When the final answer says the work is complete: hold the open items of the chat's goal record against the
    chat's tool results. What a result shows is marked done; what none shows stays open and is named under the
    answer. Like the answer check it is an extra with a step of its own, and it never fails the run."""
    from . import answer_check, goal_check
    step = None
    try:
        if not await goal_check.wanted(conn):
            return
        goal = await goals.get(conn, chat_id)
        answer = answer_check.material(acc.items)[0]
        if goal is None or not goal.open_items or len(answer) < goal_check.MIN_ANSWER:
            return
        step = await runs.add_step(conn, run_id, "decision", "goal check", {})
        await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})
        status, detail = "error", {}
        try:
            complete, detail = await goal_check.says_complete(conn, answer)
            detail = {**detail, "complete": complete}
            if complete:
                goal, marks, _, more = await goal_check.settle(conn, goal, list(goal.open_items))
                detail = {**detail, **more, "complete": True}
                said = goal_check.notice(goal, marks)
                if said:
                    acc.goal = NoticeBlock(kind="goal", text=said)
            status = "error" if detail.get("error") else "done"
        except Exception as e:  # noqa: BLE001
            detail = {**detail, "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            done = await asyncio.shield(runs.patch_step(conn, step.id, status=status, detail=detail or None, close=True))
            await asyncio.shield(hub.publish(run_id, {"type": "step_finished", "step": done.model_dump()}))
    except asyncio.CancelledError:
        pass                      # stopped while checking: the answer itself was complete
    except Exception:  # noqa: BLE001
        pass


async def _folder_check(conn, run_id: str, chat, acc: _Attempt) -> None:
    """For a run that ended by itself: the list of what it changed, and the check of its folder after it, when
    the owner set one. The run is done and stays done, whatever happens here; stopping the run ends the check."""
    try:
        acc.changes = await _changes(run_id)
        acc.changes = await checks.after(conn, run_id, chat, acc.changes)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        pass


async def _changes(run_id: str) -> ChangesBlock | None:
    """What this run changed in its work folder, when a save point was taken for it (coding mode): the list the
    owner sees under the answer and can undo. Runs for every outcome, a failed or stopped run included, and never
    raises: a fault here must not cost the run its recorded outcome."""
    try:
        found = await savepoint.after_run(run_id)
        if found and (found["files"] or found["reason"]):
            return ChangesBlock(run_id=run_id, **found)
    except Exception:  # noqa: BLE001
        pass
    return None


async def _finalize(conn_factory, conn, run_id, mid, acc, status, error, provider, model,
                    fallback_from):
    """Record the outcome and tell subscribers. The database writes are best effort;
    run_finished is published whatever happens, or the chat's viewers would wait for ever."""
    msg, totals = None, {}
    if acc.changes is None:                 # a run that did not end by itself: its list is made here, and no check follows it
        acc.changes = await _changes(run_id)
    try:
        acc.changes = checks.unfinished(run_id, acc.changes)
    except Exception:  # noqa: BLE001
        pass
    try:
        try:
            if conn is None:
                conn = await conn_factory()
            msg_status = {"done": "complete", "cancelled": "cancelled",
                          "interrupted": "interrupted"}.get(status, "error")
            kwargs = {"blocks": acc.blocks(), "status": msg_status}
            if provider:
                kwargs.update(provider=provider, model=model, fallback_from=fallback_from)
            msg = await messages.update(conn, mid, **kwargs)
            await runs.finish(conn, run_id, status, error)
            run = await runs.get(conn, run_id)
            totals = run.totals if run else {}
        except Exception as e:
            status, error = "error", error or f"{type(e).__name__}: {e}"[:300]
            try:  # one more go at recording it; a leftover 'running' row is closed by recover()
                if conn is not None:
                    msg = await messages.update(conn, mid, blocks=acc.blocks(), status="error")
                    await runs.finish(conn, run_id, status, error)
                    run = await runs.get(conn, run_id)
                    totals = run.totals if run else {}
            except Exception:
                pass
    finally:
        try:
            await hub.publish(run_id, {
                "type": "run_finished", "status": status, "error": error, "totals": totals,
                "message": msg.model_dump() if msg else None})
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
