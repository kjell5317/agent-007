"""Close a task — user marks it done.

Flips the latest anchor raw_input to status='closed' and drops the
mirrored calendar event.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.events import publish_input, publish_points, publish_task, publish_task_removed
from app.services.calendar import delete_task_event
from app.services.kotx import client as kotx_client
from app.services.kotx.notify import clear_kotx_prompt
from app.services.notifications.notify import clear_task_notifications
from app.services.points import award_for_task
from app.services.task.recur import plan_recurring_tasks
from app.services.task.vacate import vacated_commute_window, replan_vacated_window

log = logging.getLogger(__name__)

# A subtask no longer holds its parent open once it's completed or dismissed.
FINISHED = frozenset({"closed", "not_task"})


async def close_task(
    session: Session,
    task_id: uuid.UUID,
    *,
    discard_kotx: bool = True,
    award_points: bool = True,
) -> None:
    """`discard_kotx=False` is passed when the close originates from a kotx
    transition — the run is already terminal there, don't bounce a discard
    back. `award_points=False` skips the completion bonus when the close
    isn't an actual completion (a cancelled kotx run)."""
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    if getattr(task, "is_container", False):
        children = tasks_store.children(session, task_id)
        statuses = tasks_store.latest_status_for(session, [child.id for child in children])
        if any(statuses.get(child.id, "open") not in FINISHED for child in children):
            raise ValueError("Complete all subtasks before closing the parent task")
    kotx_task_id = task.kotx_task_id
    series_id = getattr(task, "recurrence_series_id", None)
    vacated = vacated_commute_window(task)
    from app.services.task.work import stop_running

    # Closing a running task completes its session.
    stop_running(session, task, reason="completed")
    # Before the orphan delete below, which takes the ledger rows with it.
    await clear_task_notifications(session, task_id)
    if kotx_task_id is not None:
        await clear_kotx_prompt(task_id)
    await _log_close(session, task)
    latest = raw_inputs_store.latest_for_task(session, task_id)
    # Award completion points before any status flip / orphan delete, so we
    # still have the task's estimation in hand. Idempotent per completion cycle and
    # best-effort — never let points bookkeeping block closing a task.
    try:
        if award_points and (latest is None or latest.status != "closed") and award_for_task(
            session, task, cycle_started_at=latest.processed_at if latest else None
        ):
            publish_points(session)
    except Exception:  # noqa: BLE001
        log.exception("points award failed · task=%s", task_id)
    if latest is not None:
        latest.status = "closed"
        task.running_since = None
        # Keep processed_at as the ingestion timestamp. This user action must
        # not make an old input count as newly arrived in the inbox.
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
    await replan_vacated_window(session, vacated)
    if discard_kotx and kotx_task_id is not None:
        try:
            await kotx_client.discard_task(kotx_task_id)
        except Exception:  # noqa: BLE001 — closing must never fail on kotx
            log.exception("kotx discard failed · kotx_task=%s", kotx_task_id)
    if series_id is not None:
        # Completing the only upcoming instance (e.g. a monthly one) must not
        # wait for the hourly sweep to plan the next — which still retries a failure.
        try:
            await plan_recurring_tasks(session, [series_id])
        except Exception:  # noqa: BLE001 — closing must never fail on the next instance
            log.exception("repeat planning failed · series=%s", series_id)
    parent_id = getattr(task, "parent_task_id", None)
    if parent_id:
        await settle_parent(session, parent_id)


async def settle_parent(session: Session, parent_id: uuid.UUID) -> None:
    """An open parent finishes with its last open subtask: it closes when any
    subtask was completed, and is dismissed when every one was dismissed."""
    children = tasks_store.children(session, parent_id)
    by_id = tasks_store.latest_status_for(session, [child.id for child in children])
    statuses = [by_id.get(child.id, "open") for child in children]
    if not children or any(status not in FINISHED for status in statuses):
        return
    if tasks_store.latest_status_for(session, [parent_id]).get(parent_id, "open") != "open":
        return
    if "closed" in statuses:
        await close_task(session, parent_id, award_points=False)
    else:
        from app.services.task.dismiss import dismiss_task

        await dismiss_task(session, parent_id)


async def _log_close(session: Session, task) -> None:
    """Record the close time against the booked slot — the duration
    observation for check-ins and closes inside the slot."""
    from app.db.clients import schedule_log as log_store
    from app.services.learn.jobs import refit_soon
    from app.services.plan.cue import booked_slot
    from app.services.plan.snapshot import features_snapshot

    try:
        slot = booked_slot(task)
        if slot is None or getattr(task, "is_container", False):
            return
        now = datetime.now(slot[0].tzinfo)
        snapshot = None
        for row in log_store.for_slot(session, task.id, task.scheduled_date):
            if row.features and row.features.get("slot"):
                snapshot = row.features
        if snapshot is None and slot[0] <= now <= slot[1] + timedelta(minutes=15):
            snapshot = await features_snapshot(session, task, slot[0], slot[1], now=now)
        log_store.add(
            session, task_id=task.id, kind="closed", actor="user",
            slot_start=task.scheduled_date, slot_end=slot[1], features=snapshot,
        )
        session.commit()
        refit_soon()
    except Exception as exc:  # noqa: BLE001 — training data must never block a close
        log.warning("close · schedule log failed task=%s err=%s", task.id, exc)
