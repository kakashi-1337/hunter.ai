"""The one shape every tool has, native or MCP."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..types import Chat, Risk, Target


@dataclass(frozen=True)
class ToolResult:
    text: str
    is_error: bool = False
    # Recognised keys: full_output_path, exit_code, process_id, keep_step_open, fallback.
    meta: dict = field(default_factory=dict)


class ToolError(Exception):
    """An expected failure; its message goes to the model as an error result."""


@dataclass(frozen=True)
class PathUse:
    path: str
    mode: Literal["read", "write"]


@dataclass(frozen=True)
class HostUse:
    host: str
    port: int | None = None
    ips: tuple[str, ...] = ()   # empty until resolved; ips[0] is where a request is sent


@dataclass(frozen=True)
class Prepared:
    """What a call will touch, worked out before the permission decision."""
    paths: tuple[PathUse, ...] = ()
    hosts: tuple[HostUse, ...] = ()
    url_path: str | None = None
    cwd: str | None = None
    grant_target: str = ""        # what "allow for this chat" covers; empty = not offered
    preview: str | None = None
    resolve_hosts: bool = False   # the runner looks up addresses once the call may proceed
    state: dict = field(default_factory=dict)


@dataclass
class ToolContext:
    chat_id: str
    run_id: str
    step_id: str
    call_id: str
    mode: str
    target: Target | None
    workdir: Path
    cancel: asyncio.Event
    prepared: Prepared | None = None


Handler = Callable[[ToolContext, dict], Awaitable[ToolResult]]
# Runs before the permission decision: reads local state only. No connection, no DNS query,
# no process, no file write. May raise ToolError.
Prepare = Callable[[ToolContext, dict], Awaitable[Prepared]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    risk: Risk
    handler: Handler
    prepare: Prepare | None = None
    source: str = "native"        # "native" or "mcp:<server>"
    expose: bool = True           # False: never offered; a call to it is an unknown tool
    writes_files: bool = False    # writes a file although its risk class is not "write"
    grant_as: str | None = None   # tools sharing this name share "allow for this chat" answers
    available: Callable[[], bool] | None = None   # asked on every offer; False: not offered now
    offered_to: Callable[[Chat], bool] | None = None   # asked on every offer, with the chat; False: not offered to that chat

    @property
    def grant_name(self) -> str:
        return self.grant_as or self.name
