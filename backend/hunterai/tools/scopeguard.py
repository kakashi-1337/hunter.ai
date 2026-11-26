"""A call that names a host of the target's out-of-scope list is not run, and the model is told so.

Until this, such a call asked the owner each time, with a warning (permissions.decide, step 6), also under
auto-approve. That holds a call back, and it leaves the answer to a prompt: on the owner's laptop on 26 November 2026
a 9B model named an out-of-scope host in all 15 runs of the bench's scope tests, and each of those would have been a
prompt to deny. A program's out-of-scope list is not a matter to decide call by call.

The guard reads the target's two lists and nothing else of the target, and it changes neither. What it decides is
the same question the permission rules ask (resolve.scope_verdict), on the same hosts: those the tool's own
`prepare` says the call reaches. It is asked before the runner gets the call, and it answers through the place the
runner has for a loop's guard (runner.call: `guard.before`), so the call ends as any call a guard stops: a tool_call
step with the refusal, the refusal as the result the model reads, and nothing sent.

It refuses what is out of scope for certain, and leaves the rest to the owner's prompt as before:

- A line with a path excludes the addresses under it. A call that says which address it asks for (web_fetch, the
  browser) is refused under such a line, and the model is told that this address is out, not the host. A command says
  no path, and the permission rules then count the line for the whole host and ask: the guard refuses nothing there,
  or a model would be told to leave a host alone that is in scope but for one path.
- It looks up no name. A lookup tells the name to a name server, and nothing was allowed yet. So a name whose
  address is on the list, and is not itself on it, is not refused here: the runner looks it up once the call may go
  on, and asks the owner with the warning then, as before.
- A host that is on neither list still asks the owner, as before. Only the out-of-scope list is refused.
- It calls `prepare` only for tools that reach out (network, execute). `prepare` of other tools names no host, and
  may cost something (the one of `delegate` asks the decision model).
- A call the runner will not take (an unknown tool, arguments that do not fit) is left to the runner, which says why.
"""
from __future__ import annotations

import asyncio
from collections.abc import Collection

from ..providers.base import ToolCall
from ..repo import chats, settings, targets
from . import on_demand, registry, resolve
from .base import ToolContext
from .loopguard import GIVE_UP

SETTING, DEFAULT = "scope_refusal", True
REACH_OUT = ("network", "execute")      # the risk classes of tools whose `prepare` names the hosts a call reaches


Out = tuple[str, str | None]     # a host, and the line with a path under which this call's address is out (None: the whole host is)


def refusal(outs: list[Out]) -> str:
    """What the model reads in place of the result of a call that named these. A host that is out as a whole is named
    as that. An address that is out because of a line with a path is named as an address, with the line: the model
    must not take the whole host for out of scope."""
    hosts = [host for host, _ in outs]
    if any(line for _, line in outs):
        which = "; ".join(f"{host}: the line {line}" if line else host for host, line in outs)
        return (f"Not run: this call goes to an address on this target's out-of-scope list ({which}), and HunterAI sends nothing "
                "there. Do not call that address again; other addresses of that host are judged for themselves. Go on with what "
                "is in scope, or say in your answer that this address is out of scope.")
    one = len(hosts) == 1
    named = hosts[0] if one else ", ".join(hosts[:-1]) + " and " + hosts[-1]
    return (f"Not run: {named} {'is' if one else 'are'} on this target's out-of-scope list, and HunterAI sends nothing to a host "
            f"on it. Do not call {'it' if one else 'them'} again. Go on with what is in scope, or say in your answer that "
            f"{'this host is' if one else 'these hosts are'} out of scope.")


async def named(conn, chat_id: str, run_id: str, tc: ToolCall, offered: Collection[str]) -> list[Out]:
    """What of the target's out-of-scope list this call reaches for certain, as its tool says; nothing when the guard
    is switched off, the chat has no target, or the call is one the runner will not take.

    For certain: a host that a line without a path excludes is out whatever the call asks of it. A line with a path
    excludes the addresses under it, so it counts only for a call that says which address it asks for (web_fetch and
    the browser do; a command does not, and the permission rules then ask the owner about the whole host)."""
    if not await settings.get(conn, SETTING, DEFAULT):
        return []
    chat = await chats.get(conn, chat_id)
    target = await targets.get(conn, chat.target_id) if chat is not None and chat.target_id else None
    if target is None:
        return []
    name, args = tc.name, tc.arguments
    if name == on_demand.CALL and on_demand.CALL in offered:        # a call through mcp_call is the tool it names
        inner = on_demand.unwrap(args, offered, registry.get)
        if isinstance(inner, str):
            return []
        name, args = inner
    tool = registry.get(name)
    if (tool is None or not tool.expose or name not in offered or tool.prepare is None or tool.risk not in REACH_OUT
            or registry.validate(tool, args)):
        return []
    ctx = ToolContext(chat_id=chat_id, run_id=run_id, step_id="", call_id=tc.id, mode=chat.mode, target=target,
                      workdir=chats.resolve_workdir(chat), cancel=asyncio.Event())
    try:
        prepared = await tool.prepare(ctx, args)
    except Exception:  # noqa: BLE001 - arguments its tool cannot look at: the runner says so to the model
        return []
    lines = [e for e in (target.scope.get("out") or []) if isinstance(e, str)]
    pathless = [e for e in lines if (entry := resolve.parse_entry(e)) is not None and entry.path is None]
    whole = resolve.scope_verdict(target.model_copy(update={"scope": {**target.scope, "out": pathless}}), prepared.hosts).out
    if prepared.url_path is None:
        return [(host, None) for host in whole]
    asked = {h.host: h for h in prepared.hosts}

    def under(host: str) -> str | None:
        return next((e for e in lines if e not in pathless
                     and resolve.matches(e, asked[host], prepared.url_path, any_address=True, loose_path=True)), None)
    return [(host, None if host in whole else under(host))
            for host in resolve.scope_verdict(target, prepared.hosts, prepared.url_path).out]


class Guard:
    """One per agent loop, around the loop's own guard (tools/loopguard.py), whose word it passes on for every call
    it does not refuse itself."""

    def __init__(self, inner):
        self.inner = inner
        self._refusals: dict[str, list[Out]] = {}       # call id -> what of the out-of-scope list that call names
        self._refused = 0                               # refusals in a row
        self._last: list[str] = []                      # the hosts of the last one, for the owner

    async def judge(self, conn, chat_id: str, run_id: str, tc: ToolCall, offered: Collection[str]) -> None:
        """Looks at a call before the runner gets it. When it names an out-of-scope host, `before` refuses it."""
        outs = await named(conn, chat_id, run_id, tc, offered)
        if outs:
            self.refuse(tc, outs)

    def refuse(self, tc: ToolCall, outs: list[Out]) -> None:
        self._refusals[tc.id] = outs

    def before(self, tc: ToolCall) -> str | None:
        outs = self._refusals.pop(tc.id, None)
        if outs is None:
            self._refused = 0
            return self.inner.before(tc)
        self._refused, self._last = self._refused + 1, [host for host, _ in outs]
        return refusal(outs)

    def after(self, tc: ToolCall, text: str, is_error: bool, exit_code: int | None = None) -> str:
        return self.inner.after(tc, text, is_error, exit_code)

    def stuck(self) -> str | None:
        """The word for the owner when the model answered refusal after refusal with one more call that names an
        out-of-scope host, or when the loop's own guard takes it to be stuck; None otherwise."""
        if self._refused >= GIVE_UP:
            return (f"Stopped: the model named a host of this target's out-of-scope list in {self._refused} calls in a row "
                    f"({', '.join(self._last)}), and none of them was run. Tell it what to do instead, or give the task to "
                    "another model.")
        return self.inner.stuck()
