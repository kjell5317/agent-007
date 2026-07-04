"""Dismiss a kotx run straight from the inbox.

A kotx transition that hasn't produced a 007 task yet (drafting / queued /
running — an informational `not_task` row) has no task to close. Dismissing it
means telling kotx to discard the run; the run's transitions stay in the inbox
log and pick up kotx's terminal `discarded` state on the next delivery.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store
from app.events import publish_kotx
from app.services.kotx import client as kotx_client

log = logging.getLogger(__name__)


async def discard_run_for_input(session: Session, raw_input_id: uuid.UUID) -> bool:
    """Discard the kotx run behind a raw input. Returns whether kotx actually
    discarded it (False when unconfigured or the run was already terminal)."""
    raw = raw_inputs_store.get(session, raw_input_id)
    if raw is None or raw.source != "kotx":
        raise LookupError("kotx input not found")
    kotx_id = (raw.source_metadata or {}).get("kotx_task_id")
    if kotx_id is None:
        raise LookupError("input is not linked to a kotx run")

    discarded = await kotx_client.discard_task(int(kotx_id))
    # kotx emits a `discarded` transition the webhook/poll ingests — nudge the
    # browser to refetch either way.
    publish_kotx()
    log.info(
        "kotx run dismissed · input=%s kotx_task=%s discarded=%s",
        raw_input_id, kotx_id, discarded,
    )
    return discarded
