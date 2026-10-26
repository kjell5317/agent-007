"""kotx reconciliation poll.

The webhook is the primary transition feed; this poll catches deliveries
missed while 007 was down. Cursor = the newest kotx raw_input we already
have, minus an overlap — envelopes dedupe on external_id, so re-fetching
is harmless. With no kotx input yet (first contact), it takes every active
run instead of a time window, so actionable runs that haven't changed in a
while still become tasks.

An unreachable kotx is a warning, not an error: the poll returns empty and
picks up from the same cursor once kotx answers again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.raw_input import RawInput
from app.events import publish_kotx
from app.services.input.create import drain
from app.services.input.kotx.source import KotxSource
from app.services.kotx import client as kotx_client
from app.services.kotx.cache import cache_kotx_task

log = logging.getLogger(__name__)

_OVERLAP = timedelta(hours=1)


def _empty(setup_error: str) -> dict:
    return {
        "fetched": 0,
        "agent_runs": 0,
        "tasks_created": 0,
        "skipped": 0,
        "errors": [{"setup": setup_error}] if setup_error else [],
    }


def _cursor(session: Session) -> datetime | None:
    latest = session.execute(
        select(func.max(RawInput.received_at)).where(RawInput.source == "kotx")
    ).scalar_one()
    return latest - _OVERLAP if latest is not None else None


async def poll(session: Session, account_key: str | None) -> dict:
    cursor = _cursor(session)
    try:
        if cursor is None:
            payloads = await kotx_client.fetch_tasks(scope="active")
        else:
            payloads = await kotx_client.fetch_tasks(updated_since=cursor, scope="all")
    except httpx.HTTPError as exc:
        log.warning("kotx poll · kotx unreachable: %s", exc)
        return _empty(f"kotx unreachable: {exc}")
    if not payloads:
        return _empty("")
    log.debug("kotx poll · %d updated tasks", len(payloads))
    summary = await drain(KotxSource(payloads), session)
    # Mirror each task's docs into the search cache (best-effort per task).
    cached = False
    for task in payloads:
        try:
            cached = await cache_kotx_task(session, task) or cached
        except Exception:  # noqa: BLE001
            log.exception("kotx poll · document cache failed · task=%s", task.get("id"))
    if cached:
        session.commit()
    # Updated runs found outside the webhook path (missed deliveries) — give
    # the browser the same refetch nudge the webhook would have sent.
    publish_kotx()
    return summary
