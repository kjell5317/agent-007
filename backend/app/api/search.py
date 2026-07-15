"""Search endpoints (stage 1 — suggest-as-you-type).

  * GET /search/suggest — cross-corpus ranked suggestions as JSON (cached).
  * GET /search/stream  — the same hits over SSE, one `hit` event per row then
    a `done` event, so the client can render results as they arrive.

Both share `run_suggest`, so the SSE stream reuses the JSON path's cache. All
matching/ranking lives in `app.services.search` + `app.db.clients.search`.
"""

from __future__ import annotations

import asyncio
import json
import logging

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.agent.chat import ChatTurn, run_chat
from app.db import SessionLocal, get_session
from app.db.clients import chats as chats_store
from app.db.schemas.search import (
    ChatConversationRead,
    ChatConversationSummary,
    ChatConversationWrite,
    ChatRequest,
    SuggestResponse,
)
from app.services.link_preview import get_link_preview
from app.services.search import run_suggest
from app.services.search.filters import ALL_CORPORA

log = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

_MAX_LIMIT = 25


def _parse_types(types: str | None) -> frozenset[str] | None:
    """`?types=task,document` → the corpus set to restrict to, or None for all.
    Unknown corpora are ignored; an all-unknown value falls back to all."""
    if not types:
        return None
    requested = {t.strip().lower() for t in types.split(",") if t.strip()}
    return frozenset(requested & ALL_CORPORA) or None


@router.get("/suggest", response_model=SuggestResponse)
async def suggest(
    q: str = Query("", max_length=256),
    limit: int | None = Query(None, ge=1, le=_MAX_LIMIT),
    types: str | None = Query(None, max_length=64),
    session: Session = Depends(get_session),
) -> SuggestResponse:
    return SuggestResponse(hits=run_suggest(session, q, limit=limit, types=_parse_types(types)))


@router.get("/stream")
async def stream(
    q: str = Query("", max_length=256),
    limit: int | None = Query(None, ge=1, le=_MAX_LIMIT),
    types: str | None = Query(None, max_length=64),
) -> EventSourceResponse:
    branches = _parse_types(types)

    # A fresh session per stream: the request-scoped `get_session` dependency
    # would be torn down before the async generator drains. The query is fast
    # and runs off the event loop in a threadpool.
    def compute() -> list[str]:
        session = SessionLocal()
        try:
            return [
                hit.model_dump_json()
                for hit in run_suggest(session, q, limit=limit, types=branches)
            ]
        finally:
            session.close()

    async def gen():
        payloads = await run_in_threadpool(compute)
        for payload in payloads:
            yield {"event": "hit", "data": payload}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(gen())


@router.post("/chat")
async def chat(body: ChatRequest) -> EventSourceResponse:
    """Stage-2/3 chatbot search over SSE. Each user turn injects the top hybrid
    hits (local + Drive) as context and the agent answers, citing hits and
    calling action tools. Events: `citations`, `token`, `tool_call`, `done`,
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
                await run_chat(session, turns, emit=emit, session_id=body.conversation_id)
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
