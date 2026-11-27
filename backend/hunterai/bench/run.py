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

from .. import config, context, cooling, problems, thermal, upstream
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
    written,
)
from ..tools import kb_tools, on_demand, resolve, runner, scopeguard
from ..tools import registry as tools
from ..tools.base import Prepared, ToolContext
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
    target: Target | None = None    # the target of the test: its out-of-scope list is held against every call
    mode: str = "pentesting"


@dataclass
class Ran:
    transcript: Transcript
    tools_offered: int
    window: int | None          # the context window the calls were given, when the model's own was too small
    seconds: float = 0.0        # the model calls took together: how fast the model is. The wait for a hot machine is not in it


# Tools that read the owner's own material: those of the knowledge base (kb_search, kb_files, kb_read). A chat nobody
# chose knowledge collections for reads the ones he named for every chat: it is offered them, and its system prompt
# tells the model to search there before it answers from memory. A bench reads no collection and has no answer for
# that search, and a model that did as it was told would be marked down for the call by every rule that counts calls.
# So they are not offered, and nothing is said of them.
OWNERS_OWN = frozenset(t.name for t in kb_tools.tools())


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
        return Request(build_system(case.mode, target), [], [], target, case.mode)
    names = [s.name for s in specs]
    system = build_system(case.mode, target, names, workdir())
    via = await upstream.told(conn, None, names)
    if via:
        system += "\n\n" + via
    if later:
        system += "\n\n" + on_demand.index(later)
    return Request(system, specs, [*names, *(t.name for t in later)], target, case.mode)


def scripted(case: Case, tc: ToolCall) -> tuple[str, bool]:
    """(what the tool answers in this test, whether that is an error): the first entry of the case's script that
    names the tool and whose word, when it has one, stands in the arguments. With no such entry, a bookkeeping
    tool is answered neutrally, and any other tool is not available."""
    return scripted_for(case, tc.name, json.dumps(tc.arguments, ensure_ascii=False, default=str))


def scripted_for(case: Case, name: str, arguments: str) -> tuple[str, bool]:
    """The same for a call that is known by its name and its arguments as JSON text: a stored call of a bench."""
    said = arguments.casefold()
    for entry in case.tools:
        if name in entry.name and (entry.args_has is None or entry.args_has.casefold() in said):
            return entry.result, entry.error
    return (NOTED, False) if is_bookkeeping(name) else (NOT_AVAILABLE, True)


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


async def _scope(request: Request, tc: ToolCall) -> tuple[list[scopeguard.Out], bool]:
    """How the scope of the test's target stands to a call the runner of a chat would take: (what of its out-of-scope
    list the scope guard of a chat would refuse the call for, whether the call goes to no host of the target at all).

    The guard asks the tool where the call goes (its `prepare`), and only a tool that reaches out (tools/scopeguard.py).
    So does this, and it adds two readings of its own:

    - A tool of HunterAI's own that reaches out says where the call goes, and the rest of its arguments is not read
      for hosts: a web search goes to a search engine, a CVE lookup to the NVD, a fetch to the host of its address. A
      tool of an MCP server cannot be looked into: the guard reads its arguments for names, and it is never said to
      go nowhere. Neither is a tool that reaches out and has nothing that says where.
    - A tool that could not read the call is read for it: every host in the arguments is one the call reaches, which
      is how the tool for commands reads a command. A command is not read by its tool here at all unless it names a
      folder that is there, because nothing is run in a bench and its working folder is never made."""
    target, tool = request.target, tools.get(tc.name)
    if target is None or tool is None:
        return [], False
    if tool.risk not in scopeguard.REACH_OUT:
        return [], True
    if tool.prepare is None:        # it reaches out and does not say where: it cannot be said to go nowhere
        return [], False
    ctx = ToolContext(chat_id="bench", run_id="bench", step_id="", call_id=tc.id, mode=request.mode, target=target,
                      workdir=workdir(), cancel=asyncio.Event())
    try:
        prepared = await tool.prepare(ctx, tc.arguments)
    except Exception:  # noqa: BLE001 - arguments its tool cannot look at
        prepared = Prepared(hosts=tuple(resolve.hosts_in(tc.arguments)))
    outs = scopeguard.reached(tool, target, prepared, tc.arguments)
    if outs or tool.source.startswith(scopeguard.SERVER):
        return outs, False
    # A tool of HunterAI's own said where the call goes. It goes to no host of the list when the permission rules of a
    # chat see none there either: they see one where the guard leaves a call to the owner (a command, and a line with
    # a path), and nobody answers in a bench.
    return [], not resolve.scope_verdict(target, prepared.hosts, prepared.url_path).out


STOPPED = "The bench was stopped before the local model {model} was called. Let the machine cool and start the bench again."


async def _cool(model: str, tell: Tell | None, stopped: str = STOPPED) -> None:
    """Waits while a part of this machine is at its limit, and raises TooHot when it does not cool in time. `stopped`
    is the sentence about what was stopped, for a caller that is no bench.

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
            f"{cooling.WAIT / 60:.0f} minutes (its limit is {still.limit:.0f} °C). {stopped.format(model=model)} "
            f"{still.setting} sets the limit in °C (off: no limit).")
    if told and tell is not None:
        await tell("running")


async def run_case(conn, provider_name: str, model: str, case: Case, tell: Tell | None = None,
                   window: int | None = None, thinking_off: bool = False) -> Ran:
    """Runs the case once and returns what it left behind. A failure of the provider is the transcript's error.
    TooHot when this machine did not cool for a model that runs on it; a cancel passes through.

    `window`: the context window the case before this one ended with on this model. A window of another size is
    another load of a local model, so during a bench it only grows.
    `thinking_off`: every call is asked without thinking, where it is else asked as the model has it."""
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
    limit = score.OUTPUT_LIMIT if here else None     # a provider elsewhere has limits of its own
    t = Transcript(calls=[], answer="", turns=0, finished=False, limit=limit)
    answer_from = 0             # where in `blocks` the answer begins: after the last result of a tool that is no bookkeeping
    last_from = 0               # where in `blocks` the last model call began to write
    spent = 0.0                 # seconds the model calls took; the wait for a hot machine is not the model's time
    quiet = thinking_off        # thinking is off: for the whole run when the bench says so, else from a turn of thinking only on
    told = False                # the model has read a refusal of the scope guard: a call it refuses from now on is one more
    in_a_row = 0                # calls in a row that the scope guard refused: a chat ends the run at scopeguard.GIVE_UP

    async def ask(wire: list[WireMessage], options: Options) -> Reply | None:
        """One model call, read into `blocks`. None when the time of the run ended in it."""
        nonlocal spent
        stream = provider.stream(model, request.system, wire, options, tools=request.specs) if with_tools \
            else provider.stream(model, request.system, wire, options)
        reply, began = Reply(), time.monotonic()
        try:
            async with asyncio.timeout(max(0.0, score.TIME_LIMIT - spent)):
                await collect(stream, blocks, taken, reply)
        except TimeoutError:
            t.timed_out = True
            return None
        finally:
            spent += time.monotonic() - began
            t.input_tokens += reply.usage.input_tokens
            t.output_tokens += reply.usage.output_tokens
        return reply

    try:
        while t.turns < case.max_turns:
            if here:
                await _cool(model, tell)
            wire = first + ([WireMessage("assistant", list(blocks))] if blocks else [])
            window = await context.roomier(provider_name, model, request.system, wire, Options(context_window=window or None),
                                           request.specs) or window
            options = Options(context_window=window or None, thinking=False if quiet else None, max_tokens=limit)
            began_at = last_from = len(blocks)
            reply = await ask(wire, options)
            if reply is not None and not quiet and problems.only_thought(blocks[began_at:], reply.stop):
                # What a chat does with such a turn (engine._call): the same question once more, thinking off. The bench
                # measures what a chat gets from the model, so it asks as a chat asks.
                quiet = t.asked_again = True
                before = written(blocks)
                if here:
                    await _cool(model, tell)
                try:
                    reply = await ask(wire, options.model_copy(update={"thinking": False}))
                except ProviderError:
                    if written(blocks) != before:
                        raise
                    reply = Reply()         # the turn stays as it was, without an answer: no failure of the run
            if reply is None:
                break
            t.turns += 1
            cut = problems.at_limit(reply.stop)     # the call was ended at a limit of length: what it wrote has no end
            if not reply.calls and not cut:
                t.finished = True
                break
            refused = False
            for sent in reply.calls:
                tc, problem = _as_a_chat_reads_it(sent, request.names)
                # What a chat does with a call that goes to a host of the target's out-of-scope list (engine: the scope
                # guard around a loop): it is not run, the model reads why, and three of them in a row end the run.
                outs, passive = ([], False) if problem else await _scope(request, tc)
                if outs:
                    problem, refused = scopeguard.refusal(outs), True
                    t.refusals, t.again, in_a_row = t.refusals + 1, t.again + told, in_a_row + 1
                elif not problem:
                    in_a_row = 0
                text, is_error = (problem, True) if problem else scripted(case, tc)
                t.calls.append(Call(tc.name, tc.arguments, problem, [host for host, _ in outs], passive))
                blocks.append(ToolResultBlock(tool_call_id=sent.id, content=text, is_error=is_error))
                if not is_bookkeeping(tc.name):
                    answer_from = len(blocks)
                if in_a_row >= scopeguard.GIVE_UP:
                    t.stopped = True
                    break
            told = told or refused
            if t.stopped:
                break
            if cut:
                t.cut_off = True
                break
    except ProviderError as e:
        t.error = e.message
    t.thinking = "\n".join(b.text for b in blocks[last_from:] if b.type == "thinking" and b.text).strip()
    if t.finished:
        t.answer = "\n".join(b.text for b in blocks[answer_from:] if b.type == "text" and b.text).strip()
    return Ran(t, len(request.names), window, spent)
