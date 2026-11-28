"""The contract every model provider implements, plus the pieces they share."""
from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict

from ..db import new_id
from ..types import (
    Block,
    FileBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ThinkingSeal:
    """Closes the thinking block the preceding ThinkingDeltas built and attaches adapter-owned
    data to it. With no open thinking block it stands for a block with empty text."""
    opaque: dict


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
    error: str | None = None      # set when the arguments could not be used; arguments is {}
    opaque: dict | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Stop:
    reason: str


Event = Union[TextDelta, ThinkingDelta, ThinkingSeal, ToolCall, Usage, Stop]

OPTION_NAMES = ("temperature", "top_p", "max_tokens", "context_window", "effort", "thinking",
                "thinking_budget", "verbosity")


# How the next token is chosen, as the servers for local models take it (Ollama under "options", llama-server as fields
# of the request). A provider names these among a model's options only when its server was seen to take them.
SAMPLING = ["top_k", "min_p", "repeat_penalty", "repeat_last_n", "presence_penalty", "frequency_penalty", "seed"]


class Options(BaseModel):
    model_config = ConfigDict(extra="ignore")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None
    top_k: int | None = None                    # only the K likeliest tokens are considered; 0: all of them
    min_p: float | None = None                  # tokens less likely than this share of the likeliest one are left out
    repeat_penalty: float | None = None         # above 1: tokens that stand in the last tokens are less likely; 1: none
    repeat_last_n: int | None = None            # how many tokens back the three penalties look
    presence_penalty: float | None = None       # for a token that stands there at all
    frequency_penalty: float | None = None      # the more often it stands there
    seed: int | None = None                     # the same seed gives the same answer to the same request
    effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    thinking: bool | None = None
    thinking_budget: int | None = None
    verbosity: Literal["low", "medium", "high"] | None = None
    # Ask for a JSON reply of this shape. Adapters that can constrain the output do so;
    # the caller still parses and validates the reply.
    json_schema: dict | None = None

    def only(self, allowed: list[str]) -> "Options":
        """A copy with every option the model does not accept cleared."""
        return Options(**{k: v for k, v in self.model_dump().items()
                          if k in allowed or k == "json_schema"})


ErrorKind = Literal["auth", "credits", "policy", "rate_limit", "unavailable", "timeout",
                    "bad_request"]
_FALLBACK = {"unavailable", "timeout", "rate_limit"}


class ProviderError(Exception):
    """`complete`: the message says what to do about it itself, so nothing is added for the owner (see problems.py).
    `provider` and `model` are filled in by the engine: which call failed."""

    def __init__(self, kind: ErrorKind, message: str, status: int | None = None,
                 retry_after: float | None = None, *, complete: bool = False):
        super().__init__(message)
        self.kind, self.message, self.status, self.retry_after = kind, message, status, retry_after
        self.complete = complete
        self.provider: str | None = None
        self.model: str | None = None

    @property
    def fallback_eligible(self) -> bool:
        return self.kind in _FALLBACK


def _body_message(body: str) -> str:
    try:
        data = json.loads(body)
        err = data.get("error", data) if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
    except (ValueError, AttributeError):
        pass
    return (body or "").strip()[:300]


def error_from_status(status: int, body: str, retry_after: str | None) -> ProviderError:
    kind: ErrorKind = {401: "auth", 402: "credits", 403: "policy", 429: "rate_limit"}.get(
        status, "unavailable" if status >= 500 else "bad_request")
    try:
        ra = float(retry_after) if retry_after is not None else None
    except ValueError:
        ra = None
    detail = _body_message(body)
    return ProviderError(kind, f"HTTP {status}" + (f": {detail}" if detail else ""), status, ra)


class ModelInfo(BaseModel):
    id: str
    label: str
    supports_images: bool = False
    options: list[str] = []
    context_window: int | None = None
    # The effort values this model takes, when they are fewer than low, medium, high, xhigh, max. A higher
    # value than the model has is sent as its highest.
    efforts: list[str] = []


@dataclass
class WireMessage:
    role: str
    blocks: list[Block]
    images: dict[str, bytes] = field(default_factory=dict)
    id: str | None = None      # the stored message this was made from, when there is one (never sent to a model)


IMAGE_OMITTED = "[image omitted: model has no image input]"


def render_file_block(b: FileBlock) -> str:
    if b.text is None:
        return f"[attached file: {b.filename}, {b.size} bytes, binary, not readable in this phase]"
    note = " (truncated)" if b.truncated else ""
    return f"[attached file: {b.filename}{note}]\n{b.text}\n[end of {b.filename}]"


def flatten_text(m: WireMessage, *, images_ok: bool = False) -> str:
    """All of a message's content as plain text. Thinking and tool blocks are not replayed."""
    parts: list[str] = []
    for b in m.blocks:
        if b.type == "text":
            parts.append(b.text)
        elif b.type == "file":
            parts.append(render_file_block(b))
        elif b.type == "image" and not images_ok:
            parts.append(f'{IMAGE_OMITTED} [the look_at_image tool can describe it: image="upload:{b.upload_id}"]')
    return "\n\n".join(p for p in parts if p)


def parse_arguments(raw: str | dict | None) -> tuple[dict, str | None]:
    """A provider's tool arguments as a dict, or ({}, why they cannot be used)."""
    if raw is None or raw == "":
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as e:
        return {}, f"arguments are not valid JSON: {e} (received: {str(raw)[:300]})"
    if not isinstance(value, dict):
        return {}, f"arguments must be a JSON object, got {type(value).__name__}"
    return value, None


ID_RE = r"^[A-Za-z0-9_-]{1,64}$"   # the charset every provider accepts for a tool-call id


def call_id() -> str:
    return "call_" + new_id()[:16]


def safe_call_id(raw: str | None, taken: Collection[str] = ()) -> str:
    """The provider's id when it is safe to use as a key and not already used, else a new one."""
    if isinstance(raw, str) and re.fullmatch(ID_RE, raw) and raw not in taken:
        return raw
    return call_id()


@dataclass
class Reply:
    """What one model call sent besides its blocks. `collect` fills it as the stream goes, so what came
    before a failure is still in it."""
    calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = Usage(0, 0)
    stop: str | None = None


def asked_again(wire: list[WireMessage], note: str | None) -> list[WireMessage]:
    """The question of a second asking: the same one, and `note` after it as one more message of the user's. The same
    list when there is no note."""
    return [*wire, WireMessage("user", [TextBlock(text=note)])] if note else wire


def written(blocks: list[Block]) -> int:
    """How much stands in `blocks`. It is more after a model call that wrote anything at all, also one that failed
    after it: `collect` adds to the last block where it can, so the number of blocks alone does not tell."""
    return len(blocks) + sum(len(getattr(b, "text", "") or "") for b in blocks)


async def collect(stream: AsyncIterator[Event], blocks: list[Block], taken: set[str], reply: Reply | None = None) -> Reply:
    """Reads one model call into `blocks`: text and thinking in pieces become one block each, a seal closes the
    thinking before it, and every tool call gets an id that is not in `taken` (and is added to it). For a loop
    that does not show a call while it comes: the subagent's, and the bench's."""
    reply = Reply() if reply is None else reply
    async for ev in stream:
        last = blocks[-1] if blocks else None
        if isinstance(ev, TextDelta):
            if last is not None and last.type == "text":
                last.text += ev.text
            else:
                blocks.append(TextBlock(text=ev.text))
        elif isinstance(ev, ThinkingDelta):
            if last is not None and last.type == "thinking" and last.opaque is None:
                last.text += ev.text
            else:
                blocks.append(ThinkingBlock(text=ev.text))
        elif isinstance(ev, ThinkingSeal):
            if last is not None and last.type == "thinking" and last.opaque is None:
                last.opaque = ev.opaque
            else:
                blocks.append(ThinkingBlock(text="", opaque=ev.opaque))
        elif isinstance(ev, ToolCall):
            tc = dataclasses.replace(ev, id=safe_call_id(ev.id, taken))
            taken.add(tc.id)
            reply.calls.append(tc)
            blocks.append(ToolCallBlock(id=tc.id, name=tc.name, arguments=tc.arguments, opaque=tc.opaque))
        elif isinstance(ev, Usage):
            reply.usage = ev
        elif isinstance(ev, Stop):
            reply.stop = ev.reason
    return reply


NO_RESULT = "[no result: the run stopped before this tool finished]"


@dataclass
class Round:
    blocks: list[Block]                 # thinking, text, tool_call, in their original order
    results: list[ToolResultBlock]      # exactly one per tool_call of the round, in call order


def rounds(m: WireMessage) -> list[Round]:
    """Split one assistant message into model turns. A turn ends with its tool results."""
    out: list[Round] = []
    blocks: list[Block] = []
    found: dict[str, ToolResultBlock] = {}
    seen_result = False

    def close() -> None:
        calls = [b for b in blocks if b.type == "tool_call"]
        results = [found.get(c.id) or ToolResultBlock(tool_call_id=c.id, content=NO_RESULT,
                                                      is_error=True) for c in calls]
        out.append(Round(list(blocks), results))

    for b in m.blocks:
        if b.type == "tool_result":
            found.setdefault(b.tool_call_id, b)
            seen_result = True
            continue
        if b.type not in ("thinking", "text", "tool_call"):
            continue
        if seen_result:
            close()
            blocks, found, seen_result = [], {}, False
        blocks.append(b)
    if blocks or not out:
        close()
    return out


class Provider(Protocol):
    name: str

    def configured(self) -> bool: ...

    async def models(self) -> list[ModelInfo]: ...

    # A provider that can use tools sets the class attribute `supports_tools = True`.
    def stream(self, model: str, system: str, messages: list[WireMessage], options: Options,
               tools: list[ToolSpec] | None = None) -> AsyncIterator[Event]: ...


async def way_out() -> str | None:
    """Where a provider's HTTP client is pointed when the owner set a proxy for the model providers: the
    local hop to it, which holds its login. None when there is none: connect as before."""
    from .. import hop, upstream
    try:
        up = upstream.of_providers()
    except upstream.ProxyError as e:
        raise ProviderError("unavailable", f"{upstream.PROVIDER_ENV} is not a proxy address: {e}. Set it again under "
                                           "Settings, or clear it.") from None
    if up is None:
        return None
    try:
        return await hop.address(up)
    except OSError as e:
        raise ProviderError("unavailable", "the local hop to the proxy for the model providers could not start "
                                           f"({type(e).__name__})") from None


def cli_env(base: dict[str, str], via: str | None) -> dict[str, str]:
    """The environment of a provider that is a program: `base`, with the way to the proxy when there is one.
    The variable that holds the proxy's own address, and its password, is never handed on."""
    from .. import upstream
    env = {k: v for k, v in base.items() if k != upstream.PROVIDER_ENV}
    return {**env, **upstream.variables(via)} if via else env


def through_proxy(e: Exception) -> str:
    """What to add to an error when a request made through the providers' proxy failed, "" when there is nothing to add."""
    import httpx

    from .. import hop
    return f" (through the proxy for the model providers: {hop.why(e)})" if isinstance(e, httpx.ProxyError) else ""

