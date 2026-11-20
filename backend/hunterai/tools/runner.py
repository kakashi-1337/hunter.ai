"""The only place a tool handler is invoked.

Every call goes validate -> prepare -> permission -> handler -> cap, and nothing a call
causes (a connection, a DNS query, a process, a file write) happens before its permission
decision.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
import unicodedata
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass

from .. import checks, config, permissions, savepoint, secrets
from ..db import new_id, now
from ..permissions import Answer, PermissionRequest, Policy, Verdict
from ..providers.base import ToolCall
from ..repo import chats, grants, runs, settings, targets
from ..types import Chat, ToolResultBlock
from ..decision import engine as decision_engine
from . import decide_tools, files, on_demand, registry, resolve
from .base import Prepared, Tool, ToolContext, ToolError

RESULT_HEAD = 22_000
RESULT_TAIL = 8_000
RESULT_CAP = 30_000
ARGS_PREVIEW = 4_000
RESULT_PREVIEW = 2_000
MAX_SAVED_OUTPUT = 20 * 1024**2   # characters

HIDDEN_WARNING = "the arguments contain invisible or direction-changing characters"
NOT_RESOLVED = ("not resolved yet: no DNS query is sent until you answer; address rules are "
                "checked after that")

GATED_RISKS = frozenset({"execute", "write", "edit"})
GATE_DEFAULT = True           # the "decision_gate" setting when the owner has not set it
ENGINE_DEFAULT = True         # the "decision_engine" setting: the built-in decide_* tools and the risk gate
DECIDE_PREFIX = "decide_"
GATE_ARGS_CHARS = 8_000
GATE_VALUE_HEAD, GATE_VALUE_TAIL = 2_500, 1_500   # of one long string argument


def gate_view(args: dict) -> tuple[str, bool]:
    """(the arguments as the gate sees them, whether anything was left out). Short arguments
    are shown whole. Long ones keep every key, and the start and the end of each long string,
    so neither the order of the keys nor padding can hide a whole argument."""
    whole = json.dumps(args, ensure_ascii=False, indent=1)
    if len(whole) <= GATE_ARGS_CHARS:
        return whole, False

    def cut(v):
        if isinstance(v, str) and len(v) > GATE_VALUE_HEAD + GATE_VALUE_TAIL:
            omitted = len(v) - GATE_VALUE_HEAD - GATE_VALUE_TAIL
            return f"{v[:GATE_VALUE_HEAD]} [... {omitted} characters omitted ...] {v[-GATE_VALUE_TAIL:]}"
        if isinstance(v, dict):
            return {k: cut(x) for k, x in v.items()}
        if isinstance(v, list):
            return [cut(x) for x in v]
        return v
    return json.dumps(cut(args), ensure_ascii=False, indent=1)[:GATE_ARGS_CHARS * 3], True

Publish = Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class Outcome:
    block: ToolResultBlock
    invalid: bool      # unknown tool or unusable arguments (counts toward the invalid cap)


def has_hidden(value) -> bool:
    """True when any string inside (keys included) carries a format or control character
    other than newline, tab, and carriage return: bidi overrides, zero-width characters,
    escape codes."""
    if isinstance(value, str):
        return any(unicodedata.category(c) == "Cf"
                   or (unicodedata.category(c) == "Cc" and c not in "\n\t\r") for c in value)
    if isinstance(value, dict):
        return any(has_hidden(k) or has_hidden(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(has_hidden(v) for v in value)
    return False


def cap(text: str, full_path: str) -> tuple[str, bool]:
    if len(text) <= RESULT_CAP:
        return text, False
    omitted = len(text) - RESULT_HEAD - RESULT_TAIL
    marker = (f"\n[... {omitted} of {len(text)} characters omitted. Full output: {full_path}. "
              f"Read it with read(path=\"{full_path}\", chunk=N) ...]\n")
    return text[:RESULT_HEAD] + marker + text[-RESULT_TAIL:], True


async def _policies(conn, chat: Chat) -> tuple[Policy, Policy]:
    return (Policy(**(await settings.get(conn, "permissions", {}) or {})),
            Policy(**(chat.permissions or {})))


async def offered_tools(conn, chat: Chat) -> list[Tool]:
    """The tools the model is told about for this chat, as things stand now."""
    global_policy, chat_policy = await _policies(conn, chat)
    offered = [t for t in registry.all()
               if permissions.offered(chat.mode, t, global_policy, chat_policy)
               and (t.available is None or t.available())
               and t.name not in (on_demand.FIND, on_demand.CALL)]        # those two come with on_demand(), when needed
    offered = [t for t in offered if t.offered_to is None or await t.offered_to(conn, chat)]
    if not await settings.get(conn, "decision_engine", ENGINE_DEFAULT):
        # The owner uses another decision engine (the JEV MCP server, say): the built-in one is not offered.
        offered = [t for t in offered if not (t.source == "native" and t.name.startswith(DECIDE_PREFIX))]
    if chat.persona_id:
        # A persona may narrow the tool set; it can never offer what the policy does not.
        from ..repo import personas
        persona = await personas.get(conn, chat.persona_id)
        if persona is not None and persona.tools:
            wanted = set(persona.tools)
            offered = [t for t in offered if t.name in wanted]
    return offered


async def for_model(conn, offered: list[Tool]) -> tuple[list[Tool], list[Tool]]:
    """(the tools to list for the model, the tools it reaches through mcp_find and mcp_call).
    Both together are what it may call. See tools/on_demand.py."""
    on = bool(await settings.get(conn, "mcp_on_demand", on_demand.DEFAULT))
    shown, deferred = on_demand.split(offered, on)
    helpers = [registry.get(on_demand.FIND), registry.get(on_demand.CALL)]
    if not deferred or None in helpers:
        return list(offered), []
    return [*shown, *helpers], deferred


def _hosts(prepared: Prepared) -> list[dict]:
    return [{"host": h.host, "port": h.port, "ips": list(h.ips)} for h in prepared.hosts]


def _paths(prepared: Prepared) -> list[dict]:
    return [{"path": p.path, "mode": p.mode} for p in prepared.paths]


async def _risk_gate(conn, run_id: str, tc: ToolCall, tool: Tool, args: dict, prepared: Prepared,
                     target, publish: Publish, parent_step_id: str | None) -> dict:
    """One decision step. Returns the gate's advice, or {"failed": True, "error"} when the
    decision model could not be reached (the call then keeps the decision it had). A reply
    that cannot be used is not a failure: it comes back invalid and recommends "confirm"."""
    step = await runs.add_step(conn, run_id, "decision", "risk gate", {"tool_call_id": tc.id},
                               parent_step_id)
    await publish({"type": "step_started", "step": step.model_dump()})
    shown, partial = gate_view(args)
    state = "\n".join([
        f"Tool: {tool.name} (risk class: {tool.risk}, source: {tool.source})",
        f"Working folder: {prepared.cwd or '(none)'}",
        f"Target: {target.name if target else '(none)'}",
        "Files: " + (", ".join(f"{p.mode} {p.path}" for p in prepared.paths) or "(none named)"),
        "Hosts: " + (", ".join(f"{h.host}:{h.port}" if h.port else h.host for h in prepared.hosts)
                     or "(none named)"),
        "Arguments" + (" (long values are cut in the middle)" if partial else "") + ":", shown])
    status, out = "error", {"failed": True, "error": "cancelled"}
    try:
        try:
            res = await decision_engine.decide(conn, state, decide_tools.GATE_QUESTIONS)
            if res.get("unreachable"):
                out = {"failed": True, "error": res.get("error", "unreachable")}
            else:
                full = decide_tools.gate_result(res)
                out = {k: full[k] for k in ("risk_score", "risk_level", "touches_sensitive",
                                            "recommendation", "recommendation_confidence",
                                            "invalid", "error", "provider", "model", "ms")
                       if k in full}
                out["clears"] = decide_tools.gate_clears(full) and not partial
                if partial:
                    out["partial"] = True
                status = "done"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as e:   # advice only: its failure is never the call's failure
            out = {"failed": True, "error": f"{type(e).__name__}: {e}"[:300]}
        return out
    finally:
        done = await asyncio.shield(runs.patch_step(
            conn, step.id, status=status, detail={"tool_call_id": tc.id, **out}, close=True))
        await asyncio.shield(publish({"type": "step_finished", "step": done.model_dump()}))


async def call(conn, *, chat_id: str, run_id: str, tc: ToolCall, offered: Collection[str],
               publish: Publish, parent_step_id: str | None = None, guard=None) -> Outcome:
    """`parent_step_id` is set for a subagent's calls: its steps hang under the delegate step.
    `guard` (tools/loopguard.py) is the calling loop's memory of failed calls."""
    began = time.monotonic()
    refused = False               # the guard stopped this call: its refusal is not counted as another failure

    async def finish(text: str, is_error: bool, invalid: bool = False) -> Outcome:
        if guard is not None and not invalid and not refused:
            text += guard.after(tc, text, is_error)
        await publish({"type": "tool_finished", "tool_call_id": tc.id, "content": text,
                       "is_error": is_error,
                       "duration_ms": int((time.monotonic() - began) * 1000)})
        return Outcome(ToolResultBlock(tool_call_id=tc.id, content=text, is_error=is_error),
                       invalid)

    async def failed(text: str, invalid: bool) -> Outcome:
        """An error that never reached a handler: one tool_call step, status error."""
        text = secrets.scrub(text)
        step = await runs.add_step(conn, run_id, "tool_call", tc.name or "(no name)",
                                   {"tool_call_id": tc.id, "error": text[:RESULT_PREVIEW]},
                                   parent_step_id)
        await publish({"type": "step_started", "step": step.model_dump()})
        step = await runs.patch_step(conn, step.id, status="error", close=True)
        await publish({"type": "step_finished", "step": step.model_dump()})
        return await finish(text, True, invalid)

    # 1. fresh state: a change the owner makes during a run applies to the next call
    chat = await chats.get(conn, chat_id)
    if chat is None:
        return await failed("This chat no longer exists.", False)
    target = await targets.get(conn, chat.target_id) if chat.target_id else None
    global_policy, chat_policy = await _policies(conn, chat)
    granted = await grants.list(conn, chat_id)

    # 2. invalid calls
    if tc.name == on_demand.CALL and not tc.error and on_demand.CALL in offered:
        # A call through mcp_call is the tool it names, from here on, for every check and every record.
        inner = on_demand.unwrap(tc.arguments, offered, registry.get)
        if isinstance(inner, str):
            return await failed(inner, True)
        tc = dataclasses.replace(tc, name=inner[0], arguments=inner[1])
    tool = registry.get(tc.name)
    if tool is not None and guard is not None and not tc.error:
        stop = guard.before(tc)
        if stop is not None:
            refused = True
            return await failed(stop, False)
    if tool is None or not tool.expose or tc.name not in offered:
        return await failed(f'Unknown tool "{tc.name}". Available tools: '
                            f'{", ".join(sorted(offered))}.', True)
    if tc.error:
        return await failed(f"Invalid call to {tc.name}: {tc.error}. Send the call again with "
                            "a JSON object that matches the tool's schema.", True)
    args = tc.arguments
    problem = registry.validate(tool, args)
    if problem:
        return await failed(f"Invalid arguments for {tc.name}: {problem}. Check the tool's "
                            "schema and send the call again.", True)

    # 3. prepare: local state only, no network
    ctx = ToolContext(chat_id=chat_id, run_id=run_id, step_id="", call_id=tc.id, mode=chat.mode,
                      target=target, workdir=chats.resolve_workdir(chat), cancel=asyncio.Event())
    try:
        prepared = await tool.prepare(ctx, args) if tool.prepare else Prepared()
    except ToolError as e:
        return await failed(str(e), False)
    except Exception as e:
        # Arguments a prepare function cannot even look at (a path longer than the system
        # allows, a URL that does not parse): the model gets an error and can try again.
        return await failed(f"Invalid arguments for {tc.name}: {type(e).__name__}: {e}. "
                            "Check the values and send the call again.", True)

    # 4. decide
    def judge(p: Prepared) -> Verdict:
        if secrets.scrub_obj(args) != args:
            return Verdict("deny", "secret", ("the arguments contain a secret HunterAI keeps: an API key value, or "
                                              "the login of a proxy",), (), False)
        v = permissions.decide(mode=chat.mode, tool=tool, prepared=p, target=target,
                               workdir=ctx.workdir, global_policy=global_policy,
                               chat_policy=chat_policy, grants=granted)
        if v.decision != "deny" and has_hidden(args):
            v = dataclasses.replace(v, decision="ask", warnings=(*v.warnings, HIDDEN_WARNING),
                                    can_allow_chat=False)
        # A change that takes away a large part of a file is the owner's to see first: no setting, grant or
        # auto-approve answers for him, and his answer covers this one call.
        large = files.large_removal(p.state)
        if v.decision != "deny" and large:
            v = dataclasses.replace(v, decision="ask", reasons=(*v.reasons, files.LARGE_REASON),
                                    warnings=(*v.warnings, large), can_allow_chat=False)
        return v

    async def with_addresses(p: Prepared) -> Prepared:
        hosts = []
        for h in p.hosts:
            hosts.append(h if h.ips else dataclasses.replace(
                h, ips=await resolve.resolve_ips(h.host)))
        return dataclasses.replace(p, hosts=tuple(hosts))

    def unresolved(p: Prepared) -> bool:
        return p.resolve_hosts and any(not h.ips for h in p.hosts)

    verdict = judge(prepared)
    if unresolved(prepared) and verdict.decision != "deny" and (
            verdict.decision == "allow" or permissions.may_resolve(
                tool=tool, prepared=prepared, target=target, global_policy=global_policy,
                chat_policy=chat_policy, grants=granted)):
        # Already allowed or already trusted: look the names up now, so the one prompt
        # (if any) shows the addresses.
        try:
            prepared = await with_addresses(prepared)
        except ToolError as e:
            return await failed(str(e), False)
        verdict = judge(prepared)

    # 4b. the risk gate: advice from the decision model. It can turn "allow" into "ask" and
    # nothing else; a call the owner already allowed for this chat is not screened again.
    gate: dict | None = None
    if (verdict.decision != "deny" and tool.risk in GATED_RISKS
            and permissions.GRANT_REASON not in verdict.reasons
            and await settings.get(conn, "decision_gate", GATE_DEFAULT)
            and await settings.get(conn, "decision_engine", ENGINE_DEFAULT)):
        gate = await _risk_gate(conn, run_id, tc, tool, args, prepared, target, publish,
                                parent_step_id)
        if not gate.get("failed"):
            advice = (f"the risk gate recommends {gate['recommendation']} "
                      f"(risk {gate['risk_score']} of 3: {gate['risk_level']})")
            if gate.get("partial"):
                advice += "; it saw only part of the arguments"
            if verdict.decision == "allow" and not gate["clears"]:
                verdict = dataclasses.replace(verdict, decision="ask", rule="gate",
                                              reasons=(*verdict.reasons, advice))

    # 5. permission step(s)
    shown: list[str] = []          # warnings the owner has seen for this call
    answers: list[str] = []
    granted_with: list[str] = []   # warnings on the prompts that were answered "for this chat"

    async def permission_step(v: Verdict, p: Prepared) -> str | None:
        """Record the decision; ask when needed. Returns the denial text, or None to go on."""
        step = await runs.add_step(conn, run_id, "permission", tool.name, {
            "tool_call_id": tc.id, "decision": v.decision, "rule": v.rule,
            "reasons": list(v.reasons), "warnings": list(v.warnings),
            **({"gate": gate} if gate else {})}, parent_step_id)
        await publish({"type": "step_started", "step": step.model_dump()})

        async def close(status: str, **detail) -> None:
            done = await runs.patch_step(conn, step.id, status=status, detail=detail or None,
                                         close=True)
            await publish({"type": "step_finished", "step": done.model_dump()})

        if v.decision == "allow":
            await close("done")
            return None
        if v.decision == "deny":
            await close("denied")
            return (f"Denied by HunterAI's permission policy ({v.rule}): "
                    f"{'; '.join(v.reasons)}. Do not repeat this call; tell the user if it "
                    "is needed.")
        req = PermissionRequest(
            id=new_id(), run_id=run_id, chat_id=chat_id, tool_call_id=tc.id, tool=tool.name,
            risk=tool.risk, source=tool.source, arguments=args, cwd=p.cwd, paths=_paths(p),
            hosts=_hosts(p), preview=p.preview,
            reasons=[*v.reasons, *([NOT_RESOLVED] if unresolved(p) else [])],
            warnings=list(v.warnings), can_allow_chat=v.can_allow_chat,
            grant_target=p.grant_target, gate=gate, created_at=now())
        await publish({"type": "permission_request", "request": req.model_dump()})

        async def resolved(answer: str, note: str = "") -> None:
            await publish({"type": "permission_resolved", "request_id": req.id,
                           "tool_call_id": tc.id, "answer": answer, "note": note})

        try:
            ans: Answer = await permissions.wait(req)
        except asyncio.CancelledError:
            await asyncio.shield(resolved("cancelled"))
            await asyncio.shield(close("cancelled", answer="cancelled"))
            raise
        except permissions.Interrupted:
            await asyncio.shield(resolved("interrupted"))
            await asyncio.shield(close("interrupted", answer="interrupted"))
            raise
        decision = ans.decision
        if decision == "allow_chat" and not v.can_allow_chat:
            decision = "allow_once"
        note = secrets.scrub(ans.note or "")
        await resolved(decision, note)
        shown.extend(v.warnings)
        answers.append(decision)
        if decision == "allow_chat":
            granted_with.extend(v.warnings)
        if decision == "deny":
            await close("denied", answer="deny", note=note)
            return "The owner denied this call." + (f" Owner's note: {note}" if note else "")
        await close("done", answer=decision, note=note)
        return None

    denial = await permission_step(verdict, prepared)
    if denial is not None:
        return await finish(denial, True)

    # 6. resolve after the call was allowed, then check the addresses
    if unresolved(prepared):
        try:
            prepared = await with_addresses(prepared)
        except ToolError as e:
            return await failed(str(e), False)
        verdict = judge(prepared)   # same policies and grants: the answer just given must
        #                             not silence a warning the owner has not seen
        if verdict.decision == "deny" or (verdict.decision == "ask" and (
                not answers or any(w not in shown for w in verdict.warnings))):
            denial = await permission_step(verdict, prepared)
            if denial is not None:
                return await finish(denial, True)
    # The last answer decides: "once" on a later prompt of the same call (the one that
    # showed the addresses) must not leave a standing grant behind.
    if answers and answers[-1] == "allow_chat" and verdict.can_allow_chat:
        await grants.add(conn, chat_id, tool.grant_name, prepared.grant_target,
                         warned=bool(granted_with))
    ctx.prepared = prepared

    # 7. run
    arguments = args
    if len(json.dumps(args, ensure_ascii=False)) > ARGS_PREVIEW:
        arguments = {"_truncated": json.dumps(args, ensure_ascii=False)[:ARGS_PREVIEW]}
    step = await runs.add_step(conn, run_id, "tool_call", tool.name, {
        "tool_call_id": tc.id, "arguments": arguments, "cwd": prepared.cwd,
        "paths": _paths(prepared), "hosts": _hosts(prepared)}, parent_step_id)
    ctx.step_id = step.id
    await publish({"type": "step_started", "step": step.model_dump()})
    await publish({"type": "tool_started", "tool_call_id": tc.id, "step_id": step.id})
    meta: dict = {}
    if chat.mode == "coding" and (tool.risk in ("write", "execute") or tool.writes_files):
        # The first call of a run that can change the work folder: keep the folder as it is, so the run can be undone.
        await savepoint.before_run(run_id, ctx.workdir)
        try:        # the folder's check, when the owner set one, on the folder as it is before anything changes
            await checks.before(conn, run_id, chat, parent_step_id)
        except Exception:  # noqa: BLE001  the check is an extra: a fault in it must not stop the call it stands before
            pass
    try:
        result = await tool.handler(ctx, args)
        text, is_error, meta = result.text, result.is_error, dict(result.meta or {})
    except ToolError as e:
        text, is_error = str(e), True
    except asyncio.CancelledError:
        ctx.cancel.set()
        done = await asyncio.shield(runs.patch_step(conn, step.id, status="cancelled", close=True))
        await asyncio.shield(publish({"type": "step_finished", "step": done.model_dump()}))
        raise
    except permissions.Interrupted:
        # The app is stopping while something inside this tool (a subagent) waited for an
        # answer: that ends the run, it is not a tool failure.
        done = await asyncio.shield(runs.patch_step(conn, step.id, status="interrupted", close=True))
        await asyncio.shield(publish({"type": "step_finished", "step": done.model_dump()}))
        raise
    except Exception as e:  # a tool bug is a result for the model, not a crashed run
        text, is_error = f"Tool {tool.name} failed: {type(e).__name__}: {e}", True

    # 8. cap and record
    text = secrets.scrub(text)
    full_path = meta.get("full_output_path")
    if len(text) > RESULT_CAP and not full_path:
        folder = config.output_dir() / run_id
        folder.mkdir(parents=True, exist_ok=True)
        target_file = folder / f"{step.id}.txt"    # named after the step, never the call id
        fd = os.open(target_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text[:MAX_SAVED_OUTPUT])
        full_path = str(target_file)
    chars = len(text)
    text, truncated = cap(text, full_path or "")
    detail: dict = {"is_error": is_error, "result_preview": text[:RESULT_PREVIEW],
                    "result_chars": chars, "truncated": truncated}
    if full_path:
        detail["full_output_path"] = full_path
    for key in ("exit_code", "process_id"):
        if meta.get(key) is not None:
            detail[key] = meta[key]
    if not is_error and any(p.mode == "write" for p in prepared.paths):
        # A file changed: what an approved command runs may have changed with it.
        cleared = await grants.clear_tools(
            conn, chat_id, [t.name for t in registry.all() if t.risk == "execute"])
        if cleared:
            detail["grants_cleared"] = cleared
    if meta.get("keep_step_open"):
        await runs.patch_step(conn, step.id, detail=detail)   # a background process closes it
    else:
        done = await runs.patch_step(conn, step.id, status="error" if is_error else "done",
                                     detail=detail, close=True)
        await publish({"type": "step_finished", "step": done.model_dump()})
    return await finish(text, is_error)
