"""Search endpoints.

  * GET /search         — the Enter search: one relevance-sorted list over
    every source the filter kind allows (local, calendar, Drive, Contacts,
    GitHub).
  * GET /search/suggest — cross-corpus ranked suggestions as JSON (cached).
  * GET /search/suggest/external — live GitHub or Calendar.

All matching/ranking lives in `app.services.search` + `app.db.clients.search`.
"""

from __future__ import annotations

import asyncio
import json
import logging

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.agent.chat import ChatTurn, run_chat
from app.config import get_settings
from app.db import SessionLocal, get_session
from app.db.models.raw_input import RawInput
from app.db.clients import chats as chats_store
from app.db.clients import raw_inputs as raw_inputs_store
from app.db.schemas.search import (
    ChatConversationRead,
    ChatConversationSummary,
    ChatConversationWrite,
    ChatRequest,
    SuggestResponse,
)
from app.services.link_preview import get_link_preview
from app.services.github import search_hits as search_github_hits
from app.services.search import run_search, run_suggest
from app.services.search.calendar import search_calendar_live
from app.services.search.drive import available_drive_formats
from app.services.search.filters import ALL_CORPORA

log = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

_MAX_LIMIT = 25


@router.get("/facets")
async def search_facets(kind: str = Query(..., pattern="^(messages|files|notes)$")) -> dict[str, list[str]]:
    """Only offer child filters backed by searchable data."""
    with SessionLocal() as session:
        if kind == "messages":
            # Generated inputs filter under their origin's source.
            hidden = ("chat", *raw_inputs_store.GENERATED_SOURCES)
            sources = session.execute(
                select(RawInput.source).where(RawInput.source.notin_(hidden)).distinct().order_by(RawInput.source)
            ).scalars().all()
            return {"options": list(sources)}
        if kind == "notes":
            sources = session.execute(text("""
                SELECT DISTINCT coalesce(r.source, 'chat') AS source
                FROM notes n LEFT JOIN raw_inputs r ON r.id = n.source_raw_input_id
                ORDER BY source
            """)).scalars().all()
            return {"options": list(sources)}
        formats = await available_drive_formats(
            session, timeout=get_settings().search_drive_timeout_seconds,
        )
        return {"options": formats}


def _parse_types(types: str | None) -> frozenset[str] | None:
    """`?types=task,document` → the corpus set to restrict to, or None for all.
    Unknown corpora are ignored; an all-unknown value falls back to all."""
    if not types:
        return None
    requested = {t.strip().lower() for t in types.split(",") if t.strip()}
    return frozenset(requested & ALL_CORPORA) or None


@router.get("", response_model=SuggestResponse)
async def search(
    q: str = Query(..., min_length=1, max_length=256),
    kind: str | None = Query(None, pattern="^(tasks|messages|events|files|contacts|notes|github)$"),
    limit: int = Query(40, ge=1, le=60),
    source: str | None = Query(None, max_length=64),
    label: str | None = Query(None, max_length=128),
    format: str | None = Query(None, max_length=32),
    status: str | None = Query(None, pattern="^(open|closed|not_task|processing|event)$"),
    recurring: bool = False,
    session: Session = Depends(get_session),
) -> SuggestResponse:
    return SuggestResponse(hits=await run_search(
        session, q, kind=kind, limit=limit, source=source, label=label, drive_format=format,
        status=status, recurring=recurring,
    ))


@router.get("/suggest", response_model=SuggestResponse)
def suggest(
    q: str = Query("", max_length=256),
    limit: int | None = Query(None, ge=1, le=_MAX_LIMIT),
    types: str | None = Query(None, max_length=64),
    exclude_source: str | None = Query(None, max_length=64),
    source: str | None = Query(None, max_length=64),
    label: str | None = Query(None, max_length=128),
    status: str | None = Query(None, pattern="^(open|closed|not_task|processing|event)$"),
    recurring: bool = False,
    session: Session = Depends(get_session),
) -> SuggestResponse:
    return SuggestResponse(hits=run_suggest(
        session, q, limit=limit, types=_parse_types(types),
        exclude_source=exclude_source, source=source, label=label,
        status=status, recurring=recurring,
    ))


@router.get("/suggest/external", response_model=SuggestResponse)
async def suggest_external(
    q: str = Query(..., min_length=1, max_length=256),
    limit: int = Query(6, ge=1, le=10),
    kind: str = Query(..., pattern="^(github|calendar)$"),
) -> SuggestResponse:
    """Live GitHub or Calendar matches, separate from fast local suggestions."""
    if kind == "github":
        return SuggestResponse(hits=await search_github_hits(q, limit=limit))
    with SessionLocal() as session:
        return SuggestResponse(hits=await search_calendar_live(
            session, q, k=limit, timeout=get_settings().search_calendar_timeout_seconds,
        ))


@router.post("/chat")
async def chat(body: ChatRequest) -> EventSourceResponse:
    """Stage-2/3 chatbot search over SSE. Each user turn injects local task
    and note hits as context; other sources are available through tools. The
    agent answers, citing hits and calling action tools. Events: `citations`,
    `token`, `tool_call`, `done`,
    `error`. A fresh session per stream (the
    request-scoped one would be torn down before the generator drains)."""
    turns = [
        ChatTurn(
            role=m.role,
            content=m.content,
            tools=tuple(t.model_dump() for t in m.tools),
        )
        for m in body.messages
        if m.content.strip() or m.tools
    ]

    async def gen():
        queue: asyncio.Queue = asyncio.Queue()
        done = object()
        session = SessionLocal()

        async def emit(event: str, data: dict) -> None:
            await queue.put((event, data))

        async def run() -> None:
            try:
                await run_chat(session, turns, emit=emit, session_id=body.conversation_id, scope=body.scope)
            except Exception as exc:  # noqa: BLE001 — surface as an SSE error frame
                log.exception("chat stream failed")
                await queue.put(("error", {"message": str(exc)}))
                await queue.put(("done", {}))
            finally:
                await queue.put(done)

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                event, data = item
                yield {"event": event, "data": json.dumps(data)}
        finally:
            task.cancel()
            session.close()

    return EventSourceResponse(gen())


@router.get("/link_preview")
async def link_preview(url: str = Query(..., max_length=2048)) -> dict:
    """Server-side unfurl for a URL in a chat answer — title/description/image.
    Fetched server-side (dodges CORS, SSRF-guarded); `preview` is null when the
    URL can't be previewed."""
    return {"preview": await get_link_preview(url)}


# --- Persisted conversations --------------------------------------------------


def _summary(row) -> ChatConversationSummary:
    return ChatConversationSummary(id=str(row.id), title=row.title, updated_at=row.updated_at)


@router.get("/chats", response_model=list[ChatConversationSummary])
def list_chats(
    limit: int = Query(5, ge=1, le=50),
    session: Session = Depends(get_session),
) -> list[ChatConversationSummary]:
    return [_summary(r) for r in chats_store.list_recent(session, limit=limit)]


@router.post("/chats", response_model=ChatConversationSummary)
def create_chat(
    body: ChatConversationWrite,
    session: Session = Depends(get_session),
) -> ChatConversationSummary:
    return _summary(chats_store.create(session, title=body.title, messages=body.messages))


@router.get("/chats/{conversation_id}", response_model=ChatConversationRead)
def get_chat(
    conversation_id: uuid.UUID,
    session: Session = Depends(get_session),
) -> ChatConversationRead:
    row = chats_store.get(session, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ChatConversationRead(
        id=str(row.id), title=row.title, updated_at=row.updated_at, messages=row.messages
    )


@router.put("/chats/{conversation_id}", response_model=ChatConversationSummary)
def update_chat(
    conversation_id: uuid.UUID,
    body: ChatConversationWrite,
    session: Session = Depends(get_session),
) -> ChatConversationSummary:
    row = chats_store.update(
        session, conversation_id, title=body.title, messages=body.messages
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return _summary(row)
