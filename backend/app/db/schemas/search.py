from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.db.clients.search import SuggestHit


class SearchHit(BaseModel):
    type: str  # task | input | note | document | drive | contact
    id: str  # the source_id an action/get tool consumes (task_id, file_id, event_id, …)
    title: str
    snippet: str | None = None
    url: str | None = None
    task_id: str | None = None
    source: str | None = None  # input source (gmail/…) or document provider (calendar/…)
    sender: str | None = None  # (last) input sender
    status: str | None = None  # task/input status, or 'event' for documents
    ts: datetime | None = None
    score: float
    # Source-specific extras surfaced to the agent in the uniform context record
    # (calendar time, drive mime type, contact emails/phones). Not every source
    # sets it; the renderer skips it when empty.
    meta: dict[str, Any] | None = None

    @classmethod
    def build(cls, hit: SuggestHit) -> "SearchHit":
        return cls(
            type=hit.type,
            id=hit.id,
            title=hit.title,
            snippet=hit.snippet,
            url=hit.url,
            task_id=hit.task_id,
            source=hit.source,
            sender=hit.sender,
            status=hit.status,
            ts=hit.ts,
            score=hit.score,
        )


class SuggestResponse(BaseModel):
    hits: list[SearchHit]


class ChatMessageIn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessageIn]
    # Optional: the persisted conversation id. Threaded to Langfuse as the trace
    # `session_id` so multi-turn conversations group in the Sessions view.
    conversation_id: str | None = None


# --- Persisted conversations (recent-chats list + reload) ---------------------


class ChatConversationWrite(BaseModel):
    title: str = ""
    # Opaque message list as the client holds it (role, content, citations,
    # tools). Stored verbatim as JSONB; the server doesn't inspect it.
    messages: list[dict] = []


class ChatConversationSummary(BaseModel):
    id: str
    title: str
    updated_at: datetime


class ChatConversationRead(ChatConversationSummary):
    messages: list[dict]
