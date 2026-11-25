"""One case on one model.

The model is sent what a new chat of the case's mode would send: the same system prompt and the same tool list.
What a bench leaves out is what belongs to one chat or to the owner's own material: a persona, a goal record,
notes from Memory, the knowledge base, skills, and the messages an owner writes into a run.

No tool is run. A call the model makes is answered from the script of the case; a call a chat would refuse before
it ran (an unknown tool, arguments that do not fit) is refused in the same words. Nothing is stored: the chat and
the target a request is built for exist only while it is built.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from .. import config, context, cooling, thermal, upstream
from ..modes import build_system
from ..providers import registry
from ..providers.base import (
    Options,
    ProviderError,
    Reply,
    ToolCall,
    ToolSpec,
    WireMessage,
    collect,
)
from ..tools import on_demand, runner
from ..tools import registry as tools
from ..types import Block, Chat, Target, TextBlock, ToolResultBlock
from . import score
from .cases import Case
from .score import Call, Transcript, is_bookkeeping

NOT_AVAILABLE = "This tool is not available in this test."
NOTED = "Noted."

Tell = Callable[[str], Awaitable[None]]     # "cooling" while a run waits for the machine, "running" when it goes on


def workdir() -> Path:
    """The working folder a request names. It is a name for the prompt: nothing is run, so it is never made."""
    return config.home() / "bench" / "work"


@dataclass
class Request:
    system: str
    specs: list[ToolSpec]       # the tools the model is listed
    names: list[str]            # every tool it may call: those, and the ones it reaches through mcp_call


@dataclass
class Ran:
    transcript: Transcript
    tools_offered: int
    window: int | None          # the context window the calls were given, when the model's own was too small
    seconds: float = 0.0        # the model calls took together: how fast the model is. The wait for a hot machine is not in it


# Tools that read the owner's own material. A chat nobody chose knowledge collections for reads the ones he named for
# every chat: it is offered kb_search, and its system prompt tells the model to search there before it answers from
# memory. A bench reads no collection and has no answer for that search, and a model that did as it was told would
# be marked down for the call by every rule that counts calls. So the tool is not offered, and nothing is said of it.
OWNERS_OWN = frozenset({"kb_search"})


async def request_for(conn, provider_name: str, model: str, case: Case) -> Request:
    """What a new chat of the case's mode on this model sends with its first message."""
    chat = Chat(id="bench", target_id=None, title="", mode=case.mode, provider=provider_name, model=model, options={},
                active_leaf_id=None, created_at="", updated_at="")
    target = None
    if case.target is not None:
        target = Target(id="bench", name=case.target.name, notes="", created_at="",
                        scope={"in": list(case.target.in_scope), "out": list(case.target.out_of_scope)})
    specs: list[ToolSpec] = []
    later = []                  # tools the model reaches through mcp_find and mcp_call, not in its list
    if getattr(registry.get(provider_name), "supports_tools", False):
        offered = [t for t in await runner.offered_tools(conn, chat) if t.name not in OWNERS_OWN]
        shown, later = await runner.for_model(conn, offered)
        specs = [ToolSpec(t.name, t.description, t.parameters) for t in shown]
    if not specs:
        return Request(build_system(case.mode, target), [], [])
    names = [s.name for s in specs]
    system = build_system(case.mode, target, names, workdir())
    via = await upstream.told(conn, None, names)
    if via:
        system += "\n\n" + via
    if later:
        system += "\n\n" + on_demand.index(later)
    return Request(system, specs, [*names, *(t.name for t in later)])


def scripted(case: Case, tc: ToolCall) -> tuple[str, bool]:
    """(what the tool answers in this test, whether that is an error): the first entry of the case's script that
    names the tool and whose word, when it has one, stands in the arguments. With no such entry, a bookkeeping
    tool is answered neutrally, and any other tool is not available."""
    said = json.dumps(tc.arguments, ensure_ascii=False, default=str).casefold()
    for entry in case.tools:
        if tc.name in entry.name and (entry.args_has is None or entry.args_has.casefold() in said):
            return entry.result, entry.error
    return (NOTED, False) if is_bookkeeping(tc.name) else (NOT_AVAILABLE, True)


def _as_a_chat_reads_it(tc: ToolCall, names: Collection[str]) -> tuple[ToolCall, str | None]:
    """The call as the tool runner of a chat reads it, and that runner's words when it would not run it (the checks
    of tools/runner.py that come before anything is run): a call through mcp_call is the tool it names; a tool
    that is not offered and arguments that cannot be used are refused."""
    if tc.name == on_demand.CALL and not tc.error and on_demand.CALL in names:
        inner = on_demand.unwrap(tc.arguments, names, tools.get)
        if isinstance(inner, str):
            return tc, inner
        tc = dataclasses.replace(tc, name=inner[0], arguments=inner[1])
    tool = tools.get(tc.name)
    if tool is None or tc.name not in names:        # a tool that is never offered is in no list of names
        return tc, f'Unknown tool "{tc.name}". Available tools: {", ".join(sorted(names))}.'
    if tc.error:
        return tc, (f"Invalid call to {tc.name}: {tc.error}. Send the call again with a JSON object that matches "
                    "the tool's schema.")
    problem = tools.validate(tool, tc.arguments)
    if problem:
        return tc, f"Invalid arguments for {tc.name}: {problem}. Check the tool's schema and send the call again."
    return tc, None


async def _cool(model: str, tell: Tell | None) -> None:
    """Waits while a part of this machine is at its limit, and raises TooHot when it does not cool in time.

    A chat waits only before a call that loads a model. A bench waits before every call of a model that runs here:
    it is hours of calls with nobody watching, and a machine that is at its limit under them should get its pause."""
    told = False

    async def hot(part: thermal.Part) -> None:
        nonlocal told
        if not told:
            told = True
            if tell is not None:
                await tell("cooling")

    still = await thermal.wait_cool(cooling.WAIT, cooling.STEP, thermal.MARGIN, hot)
    if still is not None:
        raise cooling.TooHot(
            f"The {still.name} is at {still.degrees:.0f} °C and did not cool to {still.limit - thermal.MARGIN:.0f} °C within "
            f"{cooling.WAIT / 60:.0f} minutes (its limit is {still.limit:.0f} °C). The bench was stopped before the local "
            f"model {model} was called. Let the machine cool and start the bench again. {still.setting} sets the limit "
            "in °C (off: no limit).")
    if told and tell is not None:
        await tell("running")


async def run_case(conn, provider_name: str, model: str, case: Case, tell: Tell | None = None,
                   window: int | None = None) -> Ran:
    """Runs the case once and returns what it left behind. A failure of the provider is the transcript's error.
    TooHot when this machine did not cool for a model that runs on it; a cancel passes through.

    `window`: the context window the case before this one ended with on this model. A window of another size is
    another load of a local model, so during a bench it only grows."""
    try:
        provider = registry.get(provider_name)
    except KeyError:
        return Ran(Transcript([], "", 0, False, error=f"Unknown provider: {provider_name}"), 0, window)
    request = await request_for(conn, provider_name, model, case)
    with_tools = getattr(provider, "supports_tools", False)
    here = cooling.here(provider_name, model)
    first = [WireMessage("user", [TextBlock(text=case.prompt)])]
    blocks: list[Block] = []
    taken: set[str] = set()
    t = Transcript(calls=[], answer="", turns=0, finished=False)
    answer_from = 0             # where in `blocks` the answer begins: after the last result of a tool that is no bookkeeping
    spent = 0.0                 # seconds the model calls took; the wait for a hot machine is not the model's time
    try:
        while t.turns < case.max_turns:
            if here:
                await _cool(model, tell)
            wire = first + ([WireMessage("assistant", list(blocks))] if blocks else [])
            options = Options(context_window=window) if window else Options()
            larger = await context.roomier(provider_name, model, request.system, wire, options, request.specs)
            if larger:
                window, options = larger, Options(context_window=larger)
            stream = provider.stream(model, request.system, wire, options, tools=request.specs) if with_tools \
                else provider.stream(model, request.system, wire, options)
            reply, began = Reply(), time.monotonic()
            try:
                async with asyncio.timeout(max(0.0, score.TIME_LIMIT - spent)):
                    await collect(stream, blocks, taken, reply)
            except TimeoutError:
                t.timed_out = True
                break
            finally:
                spent += time.monotonic() - began
                t.input_tokens += reply.usage.input_tokens
                t.output_tokens += reply.usage.output_tokens
            t.turns += 1
            if not reply.calls:
                t.finished = True
                break
            for sent in reply.calls:
                tc, problem = _as_a_chat_reads_it(sent, request.names)
                text, is_error = (problem, True) if problem else scripted(case, tc)
                t.calls.append(Call(tc.name, tc.arguments, problem))
                blocks.append(ToolResultBlock(tool_call_id=sent.id, content=text, is_error=is_error))
                if not is_bookkeeping(tc.name):
                    answer_from = len(blocks)
    except ProviderError as e:
        t.error = e.message
    if t.finished:
        t.answer = "\n".join(b.text for b in blocks[answer_from:] if b.type == "text" and b.text).strip()
    return Ran(t, len(request.names), window, spent)
