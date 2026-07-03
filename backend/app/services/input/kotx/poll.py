"""kotx reconciliation poll.

The webhook is the primary transition feed; this poll catches deliveries
missed while 007 was down. Cursor = the newest kotx raw_input we already
have, minus an overlap — envelopes dedupe on external_id, so re-fetching
is harmless.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.raw_input import RawInput
from app.events import publish_kotx
from app.services.input.create import drain
from app.services.input.kotx.source import KotxSource
from app.services.kotx import client as kotx_client

log = logging.getLogger(__name__)

_OVERLAP = timedelta(hours=1)
_BOOTSTRAP_LOOKBACK = timedelta(days=7)


def _empty(setup_error: str) -> dict:
    return {
        "fetched": 0,
        "agent_runs": 0,
        "tasks_created": 0,
        "skipped": 0,
        "errors": [{"setup": setup_error}] if setup_error else [],
    }


def _cursor(session: Session) -> datetime:
    latest = session.execute(
        select(func.max(RawInput.received_at)).where(RawInput.source == "kotx")
    ).scalar_one()
    if latest is None:
        return datetime.now(timezone.utc) - _BOOTSTRAP_LOOKBACK
    return latest - _OVERLAP


async def poll(session: Session, account_key: str | None) -> dict:
    payloads = await kotx_client.fetch_tasks(
        updated_since=_cursor(session), scope="all"
    )
    if not payloads:
        return _empty("")
    log.debug("kotx poll · %d updated tasks", len(payloads))
    summary = await drain(KotxSource(payloads), session)
    # Updated runs found outside the webhook path (missed deliveries) — give
    # the browser the same refetch nudge the webhook would have sent.
    publish_kotx()
    return summary
