"""The permission engine: every tool call gets allow, ask, or deny from here.

decide() is pure. It looks at what the call touches (resolved paths and hosts), not only at
the tool's risk class, and nothing in it can approve a call on the owner's behalf: steps 6
to 9 only ever raise "allow" to "ask".
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .tools import resolve
from .tools.base import HostUse, Prepared, Tool
from .types import Decision, Risk, Target

PRESETS: dict[str, dict[str, Decision]] = {
    "plan": {"read": "allow", "network": "ask", "write": "deny", "execute": "deny"},
    "pentesting": {"read": "allow", "write": "ask", "execute": "ask", "network": "ask"},
    "coding": {"read": "allow", "write": "ask", "execute": "ask", "network": "ask"},
}
HARD_DENY: dict[str, set[str]] = {"plan": {"write", "execute"}}   # no override loosens these


class Policy(BaseModel):
    """One shape for the global defaults and for a chat's own settings."""
    model_config = ConfigDict(extra="forbid")
    risk: dict[Risk, Decision] = {}
    tools: dict[str, Decision] = {}
    read_paths: list[str] = []
    write_paths: list[str] = []
    domains: list[str] = []

    @field_validator("read_paths", "write_paths")
    @classmethod
    def _absolute(cls, v: list[str]) -> list[str]:
        for p in v:
            if not os.path.isabs(os.path.expanduser(p)):
                raise ValueError(f"path must be absolute: {p}")
        return v


@dataclass(frozen=True)
class Grant:
    tool: str
    target: str
    warned: bool      # the owner saw the warnings when granting it


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    rule: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    can_allow_chat: bool


GRANT_REASON = "allowed for this chat earlier"


class PermissionRequest(BaseModel):
    id: str
    run_id: str
    chat_id: str
    tool_call_id: str
    tool: str
    risk: str
    source: str
    arguments: dict
    cwd: str | None = None
    paths: list[dict] = []        # {"path", "mode"}
    hosts: list[dict] = []        # {"host", "port", "ips"}
    preview: str | None = None
    reasons: list[str] = []
    warnings: list[str] = []
    can_allow_chat: bool = False
    grant_target: str = ""
    gate: dict | None = None      # the risk gate's advice, when it ran
    created_at: str


@dataclass(frozen=True)
class Answer:
    decision: Literal["allow_once", "allow_chat", "deny"]
    note: str = ""


def _is_shell(tool: Tool) -> bool:
    return tool.source == "native" and tool.risk == "execute"


def _mode_denies(mode: str, tool: Tool, prepared: Prepared | None) -> bool:
    hard = HARD_DENY.get(mode, ())
    if tool.risk in hard:
        return True
    if "write" in hard:
        if tool.writes_files:
            return True
        if prepared is not None and any(p.mode == "write" for p in prepared.paths):
            return True
    return False


_RULE_WORDS = {"chat-tool": "chat override for {tool}", "chat-risk": "chat setting for {risk}",
               "global-tool": "default for {tool}", "global-risk": "default for {risk}"}


def base_decision(mode: str, tool: Tool, global_policy: Policy,
                  chat_policy: Policy) -> tuple[Decision, str]:
    """Hard mode rules and the override order, without looking at what the call touches."""
    if _mode_denies(mode, tool, None):
        return "deny", "mode"
    for rule, value in (("chat-tool", chat_policy.tools.get(tool.name)),
                        ("chat-risk", chat_policy.risk.get(tool.risk)),
                        ("global-tool", global_policy.tools.get(tool.name)),
                        ("global-risk", global_policy.risk.get(tool.risk))):
        if value is not None:
            return value, rule
    return PRESETS.get(mode, PRESETS["pentesting"])[tool.risk], "preset"


def offered(mode: str, tool: Tool, global_policy: Policy, chat_policy: Policy) -> bool:
    """Whether the model is told about this tool at all."""
    return tool.expose and base_decision(mode, tool, global_policy, chat_policy)[0] != "deny"


def _inside_any(path: str, roots: Sequence[str]) -> bool:
    return any(resolve.is_inside(Path(path), Path(os.path.expanduser(r))) for r in roots)


def _in_domains(h: HostUse, domains: Sequence[str], url_path: str | None = None, *,
                any_address: bool = False) -> bool:
    return any(resolve.matches(e, h, url_path, any_address=any_address) for e in domains)


def _address_entries(domains: Sequence[str]) -> list[str]:
    """Allowlist entries that name an address (an IP, a CIDR, or localhost). Only these can
    answer the local-address question: whoever controls a name controls where it resolves."""
    out = []
    for e in domains:
        parsed = resolve.parse_entry(e)
        if parsed is not None and (parsed.network is not None or parsed.host == "localhost"
                                   or (parsed.host or "").endswith(".localhost")):
            out.append(e)
    return out


def _local_warning(h: HostUse) -> str | None:
    if resolve.is_ip(h.host):
        local = resolve.is_local(h.host)
    else:
        local = h.host == "localhost" or h.host.endswith(".localhost")
    if local:
        return f"{h.host} is an address on this machine or local network"
    bad = next((ip for ip in h.ips if resolve.is_local(ip)), None)
    if bad:
        return f"{h.host} resolves to {bad}, an address on this machine or local network"
    return None


def _in_scope_by_address(h: HostUse, target: Target | None) -> bool:
    """The address the request goes to is on an IP or CIDR entry of the in-scope list."""
    if target is None:
        return False
    literal = HostUse(h.host if resolve.is_ip(h.host) else (h.ips[0] if h.ips else ""), h.port)
    if not literal.host:
        return False
    for e in target.scope.get("in") or []:
        parsed = resolve.parse_entry(e) if isinstance(e, str) else None
        if parsed is not None and parsed.network is not None and resolve.matches(e, literal):
            return True
    return False


def decide(*, mode: str, tool: Tool, prepared: Prepared, target: Target | None, workdir: Path,
           global_policy: Policy, chat_policy: Policy, grants: Sequence[Grant]) -> Verdict:
    write_paths = [*chat_policy.write_paths, *global_policy.write_paths]
    read_paths = [*chat_policy.read_paths, *global_policy.read_paths]
    domains = [*chat_policy.domains, *global_policy.domains]
    grant = next((g for g in grants if prepared.grant_target
                  and g.tool == tool.grant_name and g.target == prepared.grant_target), None)
    warned_grant = grant is not None and grant.warned

    def deny(rule: str, reason: str) -> Verdict:
        return Verdict("deny", rule, (reason,), (), False)

    # 1. protected paths
    for p in prepared.paths:
        why = resolve.protected(Path(p.path), p.mode)
        if why:
            return deny("protected", why)
    # 2. HunterAI's own API (the native shell is handled in step 7)
    if not _is_shell(tool) and any(resolve.is_own_api(h) for h in prepared.hosts):
        return deny("own-api", "tools may not call HunterAI's own API")
    # 3. hard mode rules
    if _mode_denies(mode, tool, prepared):
        return deny("mode", f"{mode} mode never allows write or execute")
    # 4. base decision
    decision, rule = base_decision(mode, tool, global_policy, chat_policy)
    reason = (_RULE_WORDS[rule].format(tool=tool.name, risk=tool.risk) if rule in _RULE_WORDS
              else f"{mode if mode in PRESETS else 'pentesting'} mode: {decision} for {tool.risk}")
    if decision == "deny":
        return deny(rule, reason)
    reasons = [reason]
    # 5. allowlists and grants turn "ask" into "allow" (never the other way)
    if decision == "ask":
        writes = [p.path for p in prepared.paths if p.mode == "write"]
        if tool.risk == "write" and writes and all(
                _inside_any(p, write_paths) or (mode == "coding" and resolve.is_inside(Path(p), workdir))
                for p in writes):
            decision = "allow"
            reasons.append("every written path is inside an allowed folder")
        elif tool.risk == "read" and prepared.paths and all(
                _inside_any(p.path, read_paths) for p in prepared.paths):
            decision = "allow"
            reasons.append("every path is inside an allowed folder")
        elif tool.risk == "network" and prepared.hosts and all(
                _in_domains(h, domains, prepared.url_path) for h in prepared.hosts):
            decision = "allow"
            reasons.append("every host is on the domain allowlist")
        elif grant is not None:
            decision = "allow"
            reasons.append(GRANT_REASON)
    # 6-9 only ever raise "allow" to "ask"
    warnings: list[str] = []
    can_chat = bool(prepared.grant_target)
    if prepared.hosts:   # 6. target scope, whatever the risk class
        v = resolve.scope_verdict(target, prepared.hosts, prepared.url_path)
        for host in v.out:
            warnings.append(f"{host} is on this target's out-of-scope list")
            can_chat = False
        if not warned_grant:
            for line in v.unread:
                warnings.append(f"this target's out-of-scope line cannot be read, so nothing is checked against it: {line}")
            by_name = {h.host: h for h in prepared.hosts}
            for host in v.not_in:
                if not _in_domains(by_name[host], domains, prepared.url_path):
                    warnings.append(f"{host} is not in this target's in-scope list")
    if _is_shell(tool) and any(resolve.is_own_api(h) for h in prepared.hosts):   # 7.
        warnings.append("this command addresses HunterAI's own API")
        can_chat = False
    if not warned_grant:
        for p in prepared.paths:   # 8. credentials, and things that run later
            why = resolve.sensitive(Path(p.path), p.mode)
            if why:
                warnings.append(why)
        for h in prepared.hosts:   # 9. this machine and the local network
            why = _local_warning(h)
            if why and not (_in_domains(h, _address_entries(domains), any_address=True)
                            or _in_scope_by_address(h, target)):
                warnings.append(why)
    if warnings:
        decision = "ask"
    warnings = list(dict.fromkeys(warnings))
    return Verdict(decision, rule, tuple(reasons), tuple(warnings), can_chat)


def may_resolve(*, tool: Tool, prepared: Prepared, target: Target | None,
                global_policy: Policy, chat_policy: Policy, grants: Sequence[Grant]) -> bool:
    """True when the owner already trusts every name of the call, so its addresses may be
    looked up before the prompt. Otherwise no DNS query leaves before the answer."""
    if prepared.grant_target and any(g.tool == tool.grant_name and g.target == prepared.grant_target
                                     for g in grants):
        return True
    domains = [*chat_policy.domains, *global_policy.domains]
    ins = [e for e in ((target.scope.get("in") or []) if target else []) if isinstance(e, str)]
    return all(_in_domains(h, domains) or any(resolve.matches(e, h) for e in ins)
               for h in prepared.hosts)


# ---------------------------------------------------------------- pending asks
# In memory, one process. Only answer() resolves a request; nothing here approves anything.

class Interrupted(Exception):
    """The app is stopping while an ask is pending."""


_pending: dict[str, tuple[PermissionRequest, "asyncio.Future[Answer]"]] = {}


async def wait(req: PermissionRequest) -> Answer:
    fut: asyncio.Future[Answer] = asyncio.get_running_loop().create_future()
    _pending[req.id] = (req, fut)
    try:
        return await fut
    finally:
        _pending.pop(req.id, None)


def answer(run_id: str, request_id: str, ans: Answer) -> bool:
    entry = _pending.get(request_id)
    if entry is None or entry[0].run_id != run_id or entry[1].done():
        return False
    entry[1].set_result(ans)
    return True


def get(run_id: str, request_id: str) -> PermissionRequest | None:
    entry = _pending.get(request_id)
    return entry[0] if entry and entry[0].run_id == run_id and not entry[1].done() else None


def pending(run_id: str | None = None) -> list[PermissionRequest]:
    """Unanswered requests, oldest first."""
    return [req for req, fut in _pending.values()
            if not fut.done() and (run_id is None or req.run_id == run_id)]


def interrupt_all() -> set[str]:
    """Stop every waiter with Interrupted. Never produces an answer."""
    hit: set[str] = set()
    for req, fut in list(_pending.values()):
        if not fut.done():
            fut.set_exception(Interrupted())
            hit.add(req.run_id)
    return hit


def reset() -> None:
    for _, fut in _pending.values():
        if not fut.done():
            fut.cancel()
    _pending.clear()
