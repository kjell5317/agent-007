"""Polling endpoints.

Two manual triggers that the auto-poll background loop also hits:

  * `POST /sources/poll` — drain every connected ingestion source (Gmail,
    Slack, …), run the agent over each new raw input, then refresh commutes.

The per-source poll logic lives under `app.services.input.<provider>.poll`;
this file is just the HTTP surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.services.input.poll import SUPPORTED_SOURCES, poll_sources
from app.services.plan import plan_commutes

sources_router = APIRouter(prefix="/sources", tags=["sources"])


@sources_router.post("/poll")
async def poll(
    source: str | None = Query(
        None, description=f"One of {SUPPORTED_SOURCES}. Omit to poll all connected sources."
    ),
    account_key: str | None = Query(
        None, description="Narrow to a single account. Requires `source`."
    ),
    session: Session = Depends(get_session),
) -> dict:
    if account_key and not source:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "`account_key` requires `source` — pick one source to filter within.",
        )
    try:
        summary = await poll_sources(session, source=source, account_key=account_key)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    calendar_account_key = account_key if source == "gmail" else None
    summary["commutes"] = await plan_commutes(
        session,
        account_key=calendar_account_key,
    )
    return summary
