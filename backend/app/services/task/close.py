"""Close a task — user marks it done.

Flips the latest anchor raw_input to status='closed' and drops the
mirrored calendar event.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.events import publish_input, publish_points, publish_task, publish_task_removed
from app.services.calendar import delete_task_event
from app.services.kotx import client as kotx_client
from app.services.notify import clear_task_notification
from app.services.points import award_for_task
from app.services.task.vacate import vacated_commute_window, replan_vacated_window

log = logging.getLogger(__name__)


async def close_task(
    session: Session, task_id: uuid.UUID, *, discard_kotx: bool = True
) -> None:
    """`discard_kotx=False` is passed when the close originates from a kotx
    transition — the run is already terminal there, don't bounce a discard
    back."""
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    kotx_task_id = task.kotx_task_id
    vacated = vacated_commute_window(task)
    # Award completion points before any status flip / orphan delete, so we
    # still have the task's estimation in hand. Idempotent per task and
    # best-effort — never let points bookkeeping block closing a task.
    try:
        if award_for_task(session, task):
            publish_points(session)
    except Exception:  # noqa: BLE001
        log.exception("points award failed · task=%s", task_id)
    latest = raw_inputs_store.latest_for_task(session, task_id)
    if latest is not None:
        latest.status = "closed"
        latest.processed_at = datetime.now(timezone.utc)
        session.commit()
        publish_task(session, task_id)
        publish_input(session, latest.id)
    else:
        # Orphan task (no non-duplicate raw_input — e.g. anchor promoted
        # away via no_change override). With nothing to flip, drop the
        # row outright so it stops surfacing as "open" by default.
        session.delete(task)
        session.commit()
        publish_task_removed(task_id)
    await delete_task_event(session, task)
    await clear_task_notification(task_id)
    await replan_vacated_window(session, vacated)
    if discard_kotx and kotx_task_id is not None:
        try:
            await kotx_client.discard_task(kotx_task_id)
        except Exception:  # noqa: BLE001 — closing must never fail on kotx
            log.exception("kotx discard failed · kotx_task=%s", kotx_task_id)
