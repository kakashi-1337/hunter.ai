"""The contract every model provider implements, plus the pieces they share."""
from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import AsyncIterator, Literal, Protocol, Union

from pydantic import BaseModel, ConfigDict

from ..db import new_id
from ..types import Block, FileBlock, ToolResultBlock


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


class Options(BaseModel):
    model_config = ConfigDict(extra="ignore")

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None
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
    def __init__(self, kind: ErrorKind, message: str, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.kind, self.message, self.status, self.retry_after = kind, message, status, retry_after

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
