from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.db.clients.search import SuggestHit


class SearchHit(BaseModel):
    type: str  # task | input | document
    id: str
    title: str
    snippet: str | None = None
    url: str | None = None
    task_id: str | None = None
    source: str | None = None  # input source (gmail/…) or document provider (calendar/…)
    sender: str | None = None  # (last) input sender
    status: str | None = None  # task/input status, or 'event' for documents
    ts: datetime | None = None
    score: float

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
