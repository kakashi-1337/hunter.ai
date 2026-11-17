from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter, model_serializer

Risk = Literal["read", "write", "execute", "network"]
Decision = Literal["allow", "ask", "deny"]


class _OpaqueBlock(BaseModel):
    """A block with adapter-owned data that only the adapter named in opaque["provider"]
    sends back. The key is left out when unset, so older stored blocks are unchanged."""
    opaque: dict | None = None

    @model_serializer(mode="wrap")
    def _drop_unset_opaque(self, handler):
        data = handler(self)
        if data.get("opaque") is None:
            data.pop("opaque", None)
        return data


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ThinkingBlock(_OpaqueBlock):
    type: Literal["thinking"] = "thinking"
    text: str


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    upload_id: str
    mime: str


class FileBlock(BaseModel):
    type: Literal["file"] = "file"
    upload_id: str
    filename: str
    text: str | None = None
    truncated: bool = False
    size: int = 0


class ToolCallBlock(_OpaqueBlock):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


class Source(BaseModel):
    n: int
    chunk_id: str
    document_id: str
    collection_id: str
    source_uri: str
    ordinal: int
    line_start: int
    line_end: int
    score: float
    text: str


class SourcesBlock(BaseModel):
    """What the knowledge base gave an answer. Shown under it; never sent back to a model."""
    type: Literal["sources"] = "sources"
    query: str
    items: list[Source] = []
    hits: int = 0                # what the search returned; `items` are the ones that fit
    error: str | None = None


class AnswerCheckBlock(BaseModel):
    """What the decision model found when it held the final answer against the run's tool
    results: the statements that are not backed. Shown under the answer; never sent to a model."""
    type: Literal["answer_check"] = "answer_check"
    claims: list[dict] = []      # {"claim", "verdict": "unsupported" | "contradicted", "evidence"}
    checked: int = 0             # statements looked at
    supported: int = 0
    model: str = ""


class OwnerNoteBlock(BaseModel):
    """What the owner sent while the agent worked, at the place in the answer where the agent
    read it. For the transcript: the model got the words inside the tool result before it."""
    type: Literal["owner_note"] = "owner_note"
    text: str


class NoticeBlock(BaseModel):
    """Something about the answer that the owner has to know and the text does not show: it was cut off at a
    limit, or the provider's filter ended it. Shown under the answer; never sent to a model."""
    type: Literal["notice"] = "notice"
    kind: str                    # "cut_off" | "filtered"
    text: str


NOT_FOR_MODELS = ("sources", "answer_check", "owner_note", "notice")     # block types that are the owner's to read only

Block = Annotated[
    Union[TextBlock, ThinkingBlock, ImageBlock, FileBlock, ToolCallBlock, ToolResultBlock, SourcesBlock, AnswerCheckBlock,
          OwnerNoteBlock, NoticeBlock],
    Field(discriminator="type"),
]
BlockList = TypeAdapter(list[Block])

Role = Literal["user", "assistant", "system"]
Mode = Literal["plan", "pentesting", "coding"]
MessageStatus = Literal["complete", "streaming", "error", "interrupted", "cancelled"]


class Message(BaseModel):
    id: str
    chat_id: str
    parent_id: str | None
    role: Role
    blocks: list[Block]
    provider: str | None = None
    model: str | None = None
    fallback_from: str | None = None
    status: MessageStatus = "complete"
    created_at: str


class Chat(BaseModel):
    id: str
    target_id: str | None
    title: str
    mode: Mode
    provider: str
    model: str
    options: dict
    active_leaf_id: str | None
    created_at: str
    updated_at: str
    workdir: str | None = None
    permissions: dict = {}
    persona_id: str | None = None


class Target(BaseModel):
    id: str
    name: str
    notes: str
    scope: dict
    created_at: str


class SearchHit(BaseModel):
    chat_id: str
    chat_title: str
    message_id: str
    snippet: str


class Upload(BaseModel):
    id: str
    chat_id: str | None
    filename: str
    mime: str
    size: int
    sha256: str
    created_at: str


class Step(BaseModel):
    id: str
    run_id: str
    seq: int
    kind: Literal["model_call", "tool_call", "subagent", "permission", "fallback", "decision", "retrieval", "user_note", "context"]
    name: str
    status: str
    started_at: str
    ended_at: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    detail: dict = {}
    parent_step_id: str | None = None      # set on the steps of a subagent


class RunSummary(BaseModel):
    id: str
    chat_id: str
    user_message_id: str | None
    assistant_message_id: str | None
    status: Literal["running", "done", "error", "cancelled", "interrupted"]
    error: str | None = None
    started_at: str
    ended_at: str | None = None
    totals: dict = {}


class RunDetail(RunSummary):
    steps: list[Step] = []


# ---- Phase 3: jobs and knowledge collections

JobState = Literal["queued", "running", "paused", "done", "failed", "cancelled"]


class Job(BaseModel):
    id: str
    kind: str
    state: JobState
    payload: dict = {}
    checkpoint: dict = {}
    attempts: int = 0
    error: str | None = None
    progress_total: int = 0
    progress_done: int = 0
    stats: dict = {}
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class Collection(BaseModel):
    id: str
    name: str
    embedder: str
    dimension: int
    created_at: str


DocStatus = Literal["processing", "ok", "skipped", "failed"]


class Document(BaseModel):
    id: str
    collection_id: str
    source_kind: str               # upload | folder | web | git (later phases add kinds)
    source_uri: str
    sha256: str = ""
    size: int = 0
    status: DocStatus
    reason: str | None = None
    chunk_count: int = 0
    job_id: str | None = None
    created_at: str
    updated_at: str


class ChunkDraft(BaseModel):
    ordinal: int
    text: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int


class Chunk(ChunkDraft):
    id: str
    document_id: str
    collection_id: str
    embedded: bool = False


class JobItem(BaseModel):
    n: int
    job_id: str
    uri: str
    path: str
    size: int = 0
    state: Literal["pending", "done"] = "pending"


class ChatKb(BaseModel):
    chat_id: str
    collection_ids: list[str] = []
    always_retrieve: bool = False


class Hit(BaseModel):
    chunk_id: str
    document_id: str
    collection_id: str
    source_uri: str
    ordinal: int
    text: str
    line_start: int
    line_end: int
    score: float
    dense_rank: int | None = None
    keyword_rank: int | None = None
