"""Subagents: the `delegate` tool, and `swarm`, which runs several at once.

A subagent is the same agent loop on a scoped brief with a subset of the parent's tools. Its
steps are recorded under the delegate step of the parent run, its tool calls pass the same
permission engine (its asks appear in the same chat), and its answer is judged against its
own tool outputs before the parent may treat it as verified.

A swarm is several such subagents at the same time: three, unless the owner set another
number, from one to the six tasks a swarm holds (Settings; see swarm_width). A swarm member has
no fallback to the local model, no browser (a chat has one), and no tool that loads a local model.

A local model does not delegate to itself. A subagent runs on the chat's own model, so a local
model (Ollama, colibri) that delegates would hand the task to itself: it reads the task once more
from an empty context, on a machine that runs one local model at a time, and nothing is won. A
chat on a local model is therefore offered neither tool and does the work itself, and a run that
went on with the local model after the cloud one failed refuses both.

Unless the owner chose a cloud model for the subagents of a local one (Settings; cloud_stand_in).
Then both tools are offered and the subagents work on that model: without the chat's options,
which are those of its local model, and without a second try on the local model. What a subagent
reads and does goes to that provider, which is why it is the owner's choice and nothing is chosen
at the start. A cloud model that does not do the work is not tried again: after a subagent that
failed, or that answered without a single tool call, no further one is started in that run (see
_not_alone).

A swarm needs a cloud decision model as well, because that model checks every answer: a chat whose
decision model is local gets `delegate`, one at a time.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

from . import cooling, db, hub, permissions, problems
from .decision import engine as de
from .modes import build_system
from .providers import registry
from .providers.base import Options, ProviderError, Reply, ToolCall, ToolSpec, WireMessage, collect
from .repo import chats, goals, runs, settings, targets
from .tools import registry as tool_registry
from .tools import loopguard, runner
from .tools.base import Prepared, Tool, ToolContext, ToolError, ToolResult
from .types import Block, BlockList, TextBlock, ToolResultBlock

SUBAGENT_MAX_TURNS = 12
MAX_SUBAGENTS_PER_RUN = 8
SWARM_NAME = "swarm"
SWARM_WIDTH = 3                   # subagents of one swarm that run at the same time, until the owner sets another number
SWARM_MAX_TASKS = 6
LOCAL_PROVIDERS = frozenset({de.FALLBACK_PROVIDER, problems.COLIBRI})      # a model on the owner's own machine or server
ALONE = ("a local model works alone: a subagent would be that same model starting again from an empty context. "
         "Do this work yourself with your own tools, one part after another.")
NO_MORE = "Do the rest of this work yourself with your own tools: no further subagent is started in this run."
CLI_LOGINS = frozenset({"codex"})                      # the login of a command-line program, not a provider called with an API key
NOT_IN_A_SWARM = ("browser_", "look_at_image")      # one browser per chat; a tool that loads a local model
MAX_CALLS_PER_TURN = 20
EVIDENCE_CHARS = 40_000
ANSWER_CHARS = 8_000              # of the answer shown to the verifier; the evidence comes first
CRITERIA_MET = 0.5                # "accept" also needs the criteria to be more likely met than not
RESULT_EVIDENCE_CHARS = 12_000    # one full read chunk: the verifier must see what the subagent saw
NAME = "delegate"

SUBAGENT_NOTE = (
    "You are a subagent working on ONE scoped brief for an orchestrator. Do only what the brief "
    "asks, with the tools you were given. When you are done, give a final answer that states "
    "what you found and names the tool output that shows each point. Say plainly what you could "
    "not verify. Do not ask the user questions; if you are blocked, say why and stop.")

VERDICT_QUESTIONS = {
    "verdict": {"type": "choice",
                "instructions": "Judge the subagent's answer against the tool outputs and the success criteria.",
                "criteria": {
                    "accept": "Every claim in the answer is shown by the tool outputs and the success criteria are met",
                    "recheck": "Partly supported, or the criteria are not clearly met; it needs another look",
                    "reject": "Not supported by the tool outputs, contradicted by them, or no evidence at all"}},
    "criteria_met": {"type": "noul", "instructions": "Do the tool outputs show that the success criteria are met?"},
}


async def _decision(conn, run_id: str, parent_step: str, name: str, state: str, questions: dict) -> dict:
    """One decision point, recorded as a trajectory step under the delegate step."""
    step = await runs.add_step(conn, run_id, "decision", name,
                               {"questions": {k: q["instructions"] for k, q in questions.items()}},
                               parent_step)
    await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})
    status, detail = "error", {}
    try:
        try:
            res = await de.decide(conn, state[:de.MAX_STATE_CHARS], questions)
        except ValueError as e:
            res = {"answers": de.process(None, questions), "invalid": True, "error": str(e)}
        detail = {"answers": res["answers"], "invalid": bool(res.get("invalid")),
                  **{k: res[k] for k in ("provider", "model", "ms", "error") if k in res}}
        status = "error" if res.get("invalid") else "done"
        return res
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        done = await asyncio.shield(runs.patch_step(conn, step.id, status=status, detail=detail or None,
                                                    close=True))
        await asyncio.shield(hub.publish(run_id, {"type": "step_finished", "step": done.model_dump()}))


async def _model_call(conn, run_id: str, parent_step: str, provider_name: str, model: str, system: str,
                      wire: list[WireMessage], options: Options, specs: list[ToolSpec],
                      blocks: list[Block], taken: set[str]) -> list[ToolCall]:
    """One model call of the subagent; appends to `blocks`, returns the tool calls it made. Like a call of the
    run itself, one that has to load a model onto this machine waits for a hot machine to cool, and fails when it
    does not."""
    try:
        await cooling.before_call(conn, run_id, provider_name, model, parent_step)
    except cooling.TooHot as e:
        raise ProviderError("unavailable", str(e), complete=True) from None
    step = await runs.add_step(conn, run_id, "model_call", f"{provider_name}/{model}", {}, parent_step)
    await hub.publish(run_id, {"type": "step_started", "step": step.model_dump()})
    reply, status, detail = Reply(), "error", {}    # the reply is filled as the call comes: its tokens count when it fails, too
    try:
        provider = registry.get(provider_name)
        stream = provider.stream(model, system, wire, options, tools=specs) \
            if getattr(provider, "supports_tools", False) else provider.stream(model, system, wire, options)
        await collect(stream, blocks, taken, reply)
        status = "done"
        return reply.calls
    except ProviderError as e:
        detail = {"error": e.message, "kind": e.kind}
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except KeyError:
        detail = {"error": f"unknown provider {provider_name}"}
        raise ProviderError("bad_request", f"Unknown provider: {provider_name}") from None
    finally:
        if not detail and reply.stop is not None:
            detail = {"stop_reason": reply.stop}
        done = await asyncio.shield(runs.finish_step(conn, step.id, status, reply.usage.input_tokens,
                                                     reply.usage.output_tokens, detail))
        await asyncio.shield(hub.publish(run_id, {"type": "step_finished", "step": done.model_dump()}))


async def _loop(conn, ctx: ToolContext, worker: tuple[str, str], options: Options, system: str, brief: str,
                specs: list[ToolSpec], blocks: list[Block], fallback: bool = True) -> tuple[str, str | None]:
    """The subagent's agent loop on the model `worker`. Returns (status, error). `fallback` off: no second try
    on the local model (a swarm must not start local models side by side, and a local model that asked for a
    subagent is not its own subagent)."""
    names = [s.name for s in specs]
    first = [WireMessage("user", [TextBlock(text=brief)])]
    provider, model = worker
    taken: set[str] = set()
    guard = loopguard.Guard()          # this subagent's memory of calls that keep failing

    async def quiet(event: dict) -> None:
        # The chat's live view shows the delegate call, not the subagent's own tool results;
        # steps and permission requests still go out.
        if event.get("type") not in ("tool_started", "tool_finished"):
            await hub.publish(ctx.run_id, event)

    for turn in range(1, SUBAGENT_MAX_TURNS + 1):
        wire = first + ([WireMessage("assistant", list(blocks))] if blocks else [])
        try:
            calls = await _model_call(conn, ctx.run_id, ctx.step_id, provider, model, system, wire,
                                      options, specs, blocks, taken)
        except ProviderError as e:
            # Same rule as a chat's first call: the default provider may fall back once.
            if fallback and turn == 1 and not blocks and e.fallback_eligible and provider == de.DEFAULT_PROVIDER:
                from .engine import DEFAULT_FALLBACK_MODEL
                provider = de.FALLBACK_PROVIDER
                model = await settings.get(conn, "fallback_model", DEFAULT_FALLBACK_MODEL)
                try:
                    calls = await _model_call(conn, ctx.run_id, ctx.step_id, provider, model, system,
                                              wire, options, specs, blocks, taken)
                except ProviderError as e2:
                    return "error", e2.message
            else:
                return "error", e.message
        if not calls:
            return "done", None
        if turn == SUBAGENT_MAX_TURNS:
            return "max_turns", f"stopped after {SUBAGENT_MAX_TURNS} model turns"
        for n, tc in enumerate(calls):
            if n >= MAX_CALLS_PER_TURN:     # every call gets a result, or the next request is malformed
                blocks.append(ToolResultBlock(
                    tool_call_id=tc.id, is_error=True,
                    content=f"Not run: more than {MAX_CALLS_PER_TURN} tool calls in one turn. "
                            "Send the remaining calls in your next turn."))
                continue
            outcome = await runner.call(conn, chat_id=ctx.chat_id, run_id=ctx.run_id, tc=tc, offered=names,
                                        publish=quiet, parent_step_id=ctx.step_id, guard=guard)
            blocks.append(outcome.block)
    return "max_turns", f"stopped after {SUBAGENT_MAX_TURNS} model turns"


def _answer(blocks: list[Block]) -> str:
    """The subagent's final words: its text after the last tool result."""
    last = max((i for i, b in enumerate(blocks) if b.type == "tool_result"), default=-1)
    text = "\n".join(b.text for b in blocks[last + 1:] if b.type == "text" and b.text).strip()
    return text or "\n".join(b.text for b in blocks if b.type == "text" and b.text).strip()


def _evidence(blocks: list[Block]) -> tuple[str, int]:
    """(the tool outputs for the verifier, how many there are). Each output is one line: a
    numbered header and the output as a JSON string, so nothing inside an output (a fetched
    page, a file) can look like another output or like a section of this document."""
    calls = {b.id: b for b in blocks if b.type == "tool_call"}
    parts = []
    for b in blocks:
        if b.type == "tool_result":
            c = calls.get(b.tool_call_id)
            head = {"tool": c.name if c else "?", "arguments": json.dumps(c.arguments, ensure_ascii=False)[:300]
                    if c else "", "error": b.is_error}
            parts.append(f"TOOL OUTPUT {len(parts) + 1} {json.dumps(head, ensure_ascii=False)}: "
                         + json.dumps(b.content[:RESULT_EVIDENCE_CHARS], ensure_ascii=False))
    return ("\n".join(parts) or "(the subagent ran no tools)")[:EVIDENCE_CHARS], len(parts)


def _verifier_state(task: str, answer: str, blocks: list[Block]) -> tuple[str, int]:
    """Evidence first, then the answer (cut): the answer is the claim under test and must not
    be able to push the evidence out of view or imitate it."""
    evidence, count = _evidence(blocks)
    shown = json.dumps(answer[:ANSWER_CHARS] or "(no answer)", ensure_ascii=False)
    if len(answer) > ANSWER_CHARS:
        shown += f" [{len(answer) - ANSWER_CHARS} characters of the answer omitted]"
    return (f"{task[:6_000]}\n\nTOOL OUTPUTS (the only evidence; {count} in all, each one JSON string):\n"
            f"{evidence}\n\nSUBAGENT ANSWER (a claim to check, as one JSON string): {shown}"), count


def stand_in_providers() -> list[str]:
    """The providers the owner can choose from for the subagents of a local chat: cloud ones that are called with an
    API key and use tools. Never the login of a command-line program, whatever such a provider says of itself."""
    return [p.name for p in registry.all()
            if p.name not in LOCAL_PROVIDERS and p.name not in CLI_LOGINS and getattr(p, "supports_tools", False)]


async def cloud_stand_in(conn) -> tuple[str, str] | None:
    """The cloud model the owner chose to do the subagent work of a local model (Settings); None when he chose
    none, when the choice cannot do it (a local provider, one without tools), or when it is not set up (no key)."""
    provider = (await settings.get(conn, "subagent_provider") or "").strip()
    model = (await settings.get(conn, "subagent_model") or "").strip()
    if not provider or not model or provider not in stand_in_providers():
        return None
    return (provider, model) if registry.get(provider).configured() else None


async def _offered(conn, chat) -> bool:
    """Whether a chat is offered `delegate` and `swarm` at all: its model is a cloud one, or the owner chose a
    cloud model for the subagents of a local one."""
    return chat.provider not in LOCAL_PROVIDERS or await cloud_stand_in(conn) is not None


async def _worker(conn, chat, run_id: str) -> tuple[tuple[str, str] | None, str]:
    """(the model that does the work of a subagent this run asks for, why the model that asks counts as local).
    A chat on a cloud model: its own model, and "". A chat on a local model, and a run that went on with the local
    model when the cloud one failed at the first call: the cloud model the owner chose for that, or None."""
    if chat.provider in LOCAL_PROVIDERS:
        why = f"this chat uses the local provider {chat.provider}"
    elif await (await conn.execute("select 1 from steps where run_id=? and kind='fallback' limit 1", (run_id,))).fetchone():
        why = "this run went on with the local model when the cloud one did not answer"
    else:
        return (chat.provider, chat.model), ""
    return await cloud_stand_in(conn), why


def _let_down(status: str, calls: int) -> str | None:
    """What a subagent on the chosen cloud model did that shows it is not doing the work, or None: it failed, or it
    answered without a single tool call (a refusal, or an answer from memory, which can never be verified)."""
    if status == "error":
        return "The cloud model failed on a task of this run."
    if status == "done" and not calls:
        return "The cloud model answered a task of this run without doing it: it used no tool."
    return None


async def _let_down_before(conn, run_id: str) -> str | None:
    """The same, of the subagents this run started earlier."""
    rows = await (await conn.execute("select status, blocks_json from subruns where run_id=? order by created_at",
                                     (run_id,))).fetchall()
    for status, blocks in rows:
        said = _let_down(status, sum(1 for b in json.loads(blocks or "[]") if b.get("type") == "tool_call"))
        if said:
            return said
    return None


async def _not_alone(conn, chat, run_id: str) -> tuple[tuple[str, str], str]:
    """What _worker says, or ToolError where no subagent is started for the model that asks. That model is a local
    one (the chat's own, or the local model its run went on with: the tools of a run are fixed before its first
    call, so such a run was told of `delegate` and `swarm`), and the owner chose no cloud model for its subagents,
    or the one he chose has let this run down before: a local model does not try a cloud model again that does not
    do the work."""
    worker, why = await _worker(conn, chat, run_id)
    if worker is None:
        raise ToolError(f"{why}, and {ALONE}")
    if why:
        said = await _let_down_before(conn, run_id)
        if said:
            raise ToolError(f"{said} {NO_MORE}")
    return worker, why


async def _prepare_delegate(ctx: ToolContext, args: dict) -> Prepared:
    """Before the permission decision: the owner is not asked about a subagent that will not be started."""
    conn = await db.connect()
    try:
        await _not_alone(conn, await chats.get(conn, ctx.chat_id), ctx.run_id)
    finally:
        await conn.close()
    return Prepared()


async def _delegate(ctx: ToolContext, args: dict) -> ToolResult:
    return ToolResult(json.dumps(await _one(ctx, args), indent=1, ensure_ascii=False))


async def _one(ctx: ToolContext, args: dict, swarm: bool = False) -> dict:
    """Run one subagent and judge its answer. `swarm`: it is one of several running at once."""
    conn = await db.connect()
    sub_id = db.new_id()
    blocks: list[Block] = []
    status, recorded = "error", False

    async def save(final_status: str, verdict: dict | None = None) -> None:
        await conn.execute("update subruns set blocks_json=?, status=?, verdict_json=?, ended_at=? where id=?",
                           (BlockList.dump_json(blocks).decode(), final_status, json.dumps(verdict or {}),
                            db.now(), sub_id))
        await conn.commit()

    try:
        chat = await chats.get(conn, ctx.chat_id)
        # The model that does this subagent's work. A swarm asked whether one may be started before it began.
        worker, why = await (_worker(conn, chat, ctx.run_id) if swarm else _not_alone(conn, chat, ctx.run_id))
        if worker is None:
            raise ToolError(f"{why}, and {ALONE}")
        own = not why               # the chat's own model does the work; otherwise the cloud model chosen for a local one
        used = (await (await conn.execute("select count(*) from subruns where run_id=?",
                                          (ctx.run_id,))).fetchone())[0]
        if not swarm and used >= MAX_SUBAGENTS_PER_RUN:        # a swarm counted its members before it began
            raise ToolError(f"a run may start at most {MAX_SUBAGENTS_PER_RUN} subagents; do the rest yourself")
        # A subagent gets a subset of what the parent is offered, and never `delegate` or `swarm`.
        allowed = {t.name: t for t in await runner.offered_tools(conn, chat) if t.name not in (NAME, SWARM_NAME)
                   and not (swarm and t.name.startswith(NOT_IN_A_SWARM))}
        chosen = [allowed[n] for n in dict.fromkeys(args.get("tools") or []) if n in allowed]
        if not chosen:
            raise ToolError("none of the requested tools are available to a subagent; choose from: "
                            + ", ".join(sorted(allowed)))
        specs = [ToolSpec(t.name, t.description, t.parameters) for t in sorted(chosen, key=lambda t: t.name)]
        brief, criteria = args["brief"].strip(), (args.get("success_criteria") or "").strip()
        await conn.execute("insert into subruns(id,run_id,step_id,brief,status,created_at) values(?,?,?,?,?,?)",
                           (sub_id, ctx.run_id, ctx.step_id, brief, "running", db.now()))
        await conn.commit()
        recorded = True
        task = f"BRIEF:\n{brief}\n\nSUCCESS CRITERIA:\n{criteria or '(none given)'}"

        # Decision point 1: is this worth a subagent at all? Advisory.
        res = await _decision(conn, ctx.run_id, ctx.step_id, "delegate or inline", task, {"worth": {
            "type": "noul", "instructions": "Is this task large or separable enough to be worth handing to "
            "a subagent, rather than the orchestrator doing it directly in a step or two?"}})
        advice = {"verdict": "unclear" if res.get("invalid") else res["answers"]["worth"]["verdict"],
                  "probability": res["answers"]["worth"]["probability"]}
        out: dict = {"delegation_advice": advice}

        # Decision point 4: does it serve the goal record?
        goal = await goals.get(conn, ctx.chat_id)
        if goal is not None:
            from .modes import goal_block
            res = await _decision(conn, ctx.run_id, ctx.step_id, "goal drift", f"{goal_block(goal)}\n\n{task}", {
                "serves": {"type": "noul", "instructions": "Does this brief directly serve the objective?"}})
            a = res["answers"]["serves"]
            out["goal_check"] = {"verdict": "unclear" if res.get("invalid") or a["verdict"] == "uncertain"
                                 else "on track" if a["verdict"] == "yes" else "drifting",
                                 "drift_probability": round(1 - a["probability"], 3)}

        target = await targets.get(conn, chat.target_id) if chat.target_id else None
        system = build_system(chat.mode, target, [s.name for s in specs], ctx.workdir) + "\n\n" + SUBAGENT_NOTE
        from . import upstream
        via = await upstream.told(conn, chat.target_id, [s.name for s in specs])
        if via:
            system += "\n\n" + via
        # A chat's options are those of its own model. Only that model may be stood in for by the local one.
        status, error = await _loop(conn, ctx, worker, Options(**(chat.options or {})) if own else Options(), system, task,
                                    specs, blocks, fallback=own and not swarm)
        answer = _answer(blocks)

        # Decision point 2: accept, recheck, or reject, judged on the subagent's own evidence.
        state, evidence_count = _verifier_state(task, answer, blocks)
        res = await _decision(conn, ctx.run_id, ctx.step_id, "verify subagent result", state,
                              VERDICT_QUESTIONS)
        v = res["answers"]["verdict"]
        invalid = bool(res.get("invalid"))
        confident = not invalid and v["action"] == "act"
        # Verified takes all of: a finished subagent, at least one tool output, a confident
        # "accept", and criteria more likely met than not. A verdict is named only when the
        # model was confident; "accept" is named only when it also counts as verified.
        verified = (confident and v["choice"] == "accept" and status == "done" and evidence_count > 0
                    and res["answers"]["criteria_met"]["probability"] >= CRITERIA_MET)
        verdict = v["choice"] if confident and (verified or v["choice"] != "accept") else "unverified"
        if not invalid and verdict == "unverified":
            out["leaning"] = v["choice"]      # what the model leaned to, for the record only
        out.update(status=status, answer=answer, verified=verified, verdict=verdict,
                   verdict_confidence=v["confidence"], probabilities=v["probabilities"],
                   criteria_met=res["answers"]["criteria_met"]["probability"],
                   calls=sum(1 for b in blocks if b.type == "tool_call"))
        if error:
            out["error"] = error
        out["note"] = ("Verified against the subagent's own tool outputs." if verified else
                       "UNVERIFIED: treat this answer as a claim, not a fact. Check it yourself with a tool "
                       "before relying on it, and label it unverified if you report it.")
        said = None if own else _let_down(status, out["calls"])
        if said:                    # the local model that asked is told now, not at its next try
            out["note"] += f" {said} {NO_MORE}"
        await save(status, {k: out[k] for k in ("verified", "verdict", "verdict_confidence", "probabilities",
                                                "criteria_met")})
        ordered = {k: out[k] for k in ("status", "answer", "verified", "verdict", "verdict_confidence",
                                       "probabilities", "criteria_met", "calls", "note") if k in out}
        ordered.update({k: v for k, v in out.items() if k not in ordered})
        return ordered
    except asyncio.CancelledError:
        if recorded:
            await asyncio.shield(save("cancelled"))
        raise
    except permissions.Interrupted:
        if recorded:
            await asyncio.shield(save("interrupted"))
        raise
    except ToolError:
        if recorded:
            await save("error")
        raise
    finally:
        await conn.close()


async def swarm_width(conn) -> int:
    """How many subagents of one swarm run at the same time: the owner's setting, from one to
    the tasks a swarm holds. What each of them runs (its tools, its model calls) loads this
    machine and the model provider, which is why it is the owner's to set."""
    try:
        width = int(await settings.get(conn, "swarm_width", SWARM_WIDTH))
    except (TypeError, ValueError):
        width = SWARM_WIDTH
    return max(1, min(SWARM_MAX_TASKS, width))


async def _cloud_only(conn, chat, run_id: str) -> None:
    """ToolError unless both models a swarm calls are cloud models: the one that does the work (see _not_alone) and
    the decision model, which checks every answer."""
    await _not_alone(conn, chat, run_id)
    provider, model = await de.decision_model(conn)
    if provider in LOCAL_PROVIDERS:
        raise ToolError(f"a swarm needs a cloud decision model too (it checks every subagent's answer); the decision model "
                        f"is the local {provider}/{model}. Choose a cloud one in Settings, or use delegate.")


async def _prepare_swarm(ctx: ToolContext, args: dict) -> Prepared:
    conn = await db.connect()
    try:
        await _cloud_only(conn, await chats.get(conn, ctx.chat_id), ctx.run_id)
    finally:
        await conn.close()
    return Prepared()


async def _swarm(ctx: ToolContext, args: dict) -> ToolResult:
    tasks = args["tasks"]
    conn = await db.connect()
    try:
        await _cloud_only(conn, await chats.get(conn, ctx.chat_id), ctx.run_id)
        used = (await (await conn.execute("select count(*) from subruns where run_id=?", (ctx.run_id,))).fetchone())[0]
        if used + len(tasks) > MAX_SUBAGENTS_PER_RUN:
            raise ToolError(f"a run may start at most {MAX_SUBAGENTS_PER_RUN} subagents and {used} were started: "
                            f"{MAX_SUBAGENTS_PER_RUN - used} are left")
        steps = []
        for n, task in enumerate(tasks, 1):       # one step per member: what each does is recorded under its own
            step = await runs.add_step(conn, ctx.run_id, "subagent", f"subagent {n} of {len(tasks)}",
                                       {"brief": task["brief"][:500]}, ctx.step_id)
            await hub.publish(ctx.run_id, {"type": "step_started", "step": step.model_dump()})
            steps.append(step)
        gate = asyncio.Semaphore(await swarm_width(conn))
        # The members close their steps on this one connection, one at a time. Two that overlap leave one's read
        # open while the other's update begins, and SQLite refuses a write that starts from an open read at once
        # ("database is locked", no waiting) whenever anything else is writing: the whole swarm's answer was lost.
        one_at_a_time = asyncio.Lock()

        async def close(n: int, status: str, detail: dict):
            async with one_at_a_time:
                return await runs.patch_step(conn, steps[n].id, status=status, close=True, detail=detail)

        async def member(n: int, task: dict) -> dict:
            status, out = "error", {}
            try:
                async with gate:
                    out = await _one(dataclasses.replace(ctx, step_id=steps[n].id), task, swarm=True)
                status = "done"
            except ToolError as e:
                out = {"status": "error", "error": str(e), "verified": False}
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            finally:
                done = await asyncio.shield(close(n, status, {
                    "brief": task["brief"][:500], **{k: out[k] for k in ("status", "verified", "verdict") if k in out}}))
                await asyncio.shield(hub.publish(ctx.run_id, {"type": "step_finished", "step": done.model_dump()}))
            return {"task": n + 1, **out}

        results = await asyncio.gather(*(member(n, t) for n, t in enumerate(tasks)))
        verified = sum(1 for r in results if r.get("verified"))
        return ToolResult(json.dumps({
            "results": list(results),
            "note": f"{verified} of {len(results)} answers are verified against their own tool outputs. Treat every other "
                    "one as a claim: check it with a tool before you rely on it."}, indent=1, ensure_ascii=False))
    finally:
        await conn.close()


def swarm_tool() -> Tool:
    brief = {"type": "object", "properties": {
        "brief": {"type": "string", "minLength": 1, "maxLength": 8000},
        "success_criteria": {"type": "string", "maxLength": 2000},
        "tools": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}},
        "required": ["brief", "tools"], "additionalProperties": False}
    return Tool(SWARM_NAME, (
        "Hand several INDEPENDENT tasks to subagents that work at the same time (a few at once; the rest wait their turn). Use it "
        "when the work splits into parts that do not need each other's results: one host each, one feature each, one "
        "file each. Every task is a delegate brief: self-contained (a subagent sees nothing else), with success "
        "criteria and the tools it may use. Subagents of a swarm have no browser and no image tool. Each result carries "
        "its own verdict: only verified=true may be reported as fact. When a swarm is refused because the decision "
        "model is a local one, use delegate."),
        {"type": "object", "properties": {
            "tasks": {"type": "array", "items": brief, "minItems": 2, "maxItems": SWARM_MAX_TASKS}},
         "required": ["tasks"], "additionalProperties": False},
        "read", _swarm, prepare=_prepare_swarm, offered_to=_offered)


def tool() -> Tool:
    return Tool(NAME, (
        "Hand ONE large or separable task to a subagent. Use it only when the work is big enough to be "
        "worth it; do small tasks yourself. Give a self-contained brief (the subagent sees nothing else), "
        "success criteria, and the tools it may use (a subset of yours). The result carries a verdict: "
        "only verified=true may be reported as fact."),
        {"type": "object", "properties": {
            "brief": {"type": "string", "minLength": 1, "maxLength": 8000},
            "success_criteria": {"type": "string", "maxLength": 2000},
            "tools": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20}},
         "required": ["brief", "tools"], "additionalProperties": False},
        "read", _delegate, prepare=_prepare_delegate, offered_to=_offered)
