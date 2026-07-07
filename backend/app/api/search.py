"""Search endpoints (stage 1 — suggest-as-you-type).

  * GET /search/suggest — cross-corpus ranked suggestions as JSON (cached).
  * GET /search/stream  — the same hits over SSE, one `hit` event per row then
    a `done` event, so the client can render results as they arrive.

Both share `run_suggest`, so the SSE stream reuses the JSON path's cache. All
matching/ranking lives in `app.services.search` + `app.db.clients.search`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.db import SessionLocal, get_session
from app.db.schemas.search import SuggestResponse
from app.services.search import run_suggest

router = APIRouter(prefix="/search", tags=["search"])

_MAX_LIMIT = 25


@router.get("/suggest", response_model=SuggestResponse)
async def suggest(
    q: str = Query("", max_length=256),
    limit: int | None = Query(None, ge=1, le=_MAX_LIMIT),
    session: Session = Depends(get_session),
) -> SuggestResponse:
    return SuggestResponse(hits=run_suggest(session, q, limit=limit))


@router.get("/stream")
async def stream(
    q: str = Query("", max_length=256),
    limit: int | None = Query(None, ge=1, le=_MAX_LIMIT),
) -> EventSourceResponse:
    # A fresh session per stream: the request-scoped `get_session` dependency
    # would be torn down before the async generator drains. The query is fast
    # and runs off the event loop in a threadpool.
    def compute() -> list[str]:
        session = SessionLocal()
        try:
            return [hit.model_dump_json() for hit in run_suggest(session, q, limit=limit)]
        finally:
            session.close()

    async def gen():
        payloads = await run_in_threadpool(compute)
        for payload in payloads:
            yield {"event": "hit", "data": payload}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(gen())
