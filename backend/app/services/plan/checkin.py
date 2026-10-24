"""End-of-slot check-ins and "Still working" extensions.

Fifteen minutes after a task's slot ends, one notification asks what
happened — Done / Still working / Not done. The answer is the training
signal for the preference model and the duration correction:

* Done — close the task, completion time = slot end.
* Still working — no further notification; the calendar event grows by
  15 minutes right away and again every 15 minutes from the answer until
  the task is marked done in the app (actual duration = done time − slot
  start), stopping short of the next calendar event. After 2× its booked
  duration it stops and counts as abandoned.
* Not done — the penalty is deducted silently and the task is re-planned.
* No answer within 2 h — same as Not done, but excluded from learning.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import schedule_log as log_store
from app.db.clients import tasks as tasks_store
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

CHECKIN_DELAY = timedelta(minutes=15)
NO_RESPONSE_AFTER = timedelta(hours=2)
EXTENSION_STEP = timedelta(minutes=15)
ABANDON_FACTOR = 2
# Slots that ended longer ago than this (missed while the service was down,
# or from before check-ins existed) are settled as "no response" without
# pinging the phone about something hours old.
CHECKIN_MAX_AGE = timedelta(hours=6)
# A slot that never reached the calendar is re-planned at most this often.
UNBOOKED_RETRY = timedelta(hours=1)
_unbooked_retry: dict[uuid.UUID, datetime] = {}

ACTION_CHECKIN_DONE = "CHECKIN_DONE"
ACTION_CHECKIN_STILL_WORKING = "CHECKIN_STILL_WORKING"
ACTION_CHECKIN_NOT_DONE = "CHECKIN_NOT_DONE"
CHECKIN_ACTIONS = frozenset(
    {ACTION_CHECKIN_DONE, ACTION_CHECKIN_STILL_WORKING, ACTION_CHECKIN_NOT_DONE}
)


# --- Pure timing helpers (shared with the solver input) -----------------------


def booked_slot(task) -> tuple[datetime, datetime] | None:
    from app.services.plan.schedule import _duration_minutes

    if task.scheduled_date is None:
        return None
    start = to_user_tz(task.scheduled_date)
    return start, start + timedelta(minutes=_duration_minutes(task, get_settings()))


def awaiting_checkin(slot_end: datetime, now: datetime, resolved: bool) -> bool:
    """A slot that ended but whose outcome is still open: the check-in hasn't
    gone out yet or hasn't been answered in time."""
    return not resolved and slot_end <= now < slot_end + CHECKIN_DELAY + NO_RESPONSE_AFTER


def running_end(start: datetime, booked_end: datetime, now: datetime, since: datetime) -> datetime:
    """Where a running extension's event ends at `now`: the next 15-minute
    step past now, counted from the "Still working" answer at `since`,
    capped at the abandon point and never short of the booked end."""
    cap = start + ABANDON_FACTOR * (booked_end - start)
    steps = math.floor(max(now - since, timedelta(0)) / EXTENSION_STEP) + 1
    return max(booked_end, min(cap, since + steps * EXTENSION_STEP))


def abandoned(start: datetime, booked_end: datetime, now: datetime) -> bool:
    return now >= start + ABANDON_FACTOR * (booked_end - start)


def preempted(slot: tuple[datetime, datetime], overtime: list[tuple[datetime, datetime]]) -> bool:
    """A slot that a running task's overtime grew into didn't happen — the
    user was still on the running task — so it must move even once its
    time has begun or passed. The check-in arrives 15 minutes after the
    booked end, so the slot right after it has always begun by then."""
    return any(lo < slot[1] and slot[0] < hi for lo, hi in overtime)


# --- Sweep -----------------------------------------------------------------------


async def checkin_overdue_slots_once() -> dict[str, int]:
    """Send due check-ins, settle unanswered ones, drive running extensions."""
    from app.db import SessionLocal

    summary = {"sent": 0, "no_response": 0, "extended": 0, "abandoned": 0}
    with SessionLocal() as session:
        now = datetime.now(user_tz())
        summary["sent"], stale = await _send_checkins(session, now)
        summary["no_response"] = await _settle_unanswered(session, now, stale)
        extended, dropped = await _extend_running(session, now)
        summary["extended"], summary["abandoned"] = extended, dropped
    return summary


async def _send_checkins(session: Session, now: datetime) -> tuple[int, list]:
    from app.services.notify import notify_checkin
    from app.services.plan.snapshot import features_snapshot

    from app.services.plan.schedule import schedule_task

    sent = 0
    stale: list = []
    candidates = tasks_store.overdue_scheduled_open(session, cutoff=now - CHECKIN_DELAY)
    candidates = [t for t in candidates if t.running_since is None and t.schedule_state == "placed"]
    # A slot that never reached the calendar (a new task whose placement
    # failed keeps its provisional creation-time slot) was never offered to
    # the user: re-plan it instead of asking about it.
    for task in [t for t in candidates if not t.calendar_event_id]:
        if _unbooked_retry.get(task.id, now) > now:
            continue
        if await schedule_task(session, task, notify=False) is None:
            _unbooked_retry[task.id] = now + UNBOOKED_RETRY
        else:
            _unbooked_retry.pop(task.id, None)
    candidates = [t for t in candidates if t.calendar_event_id]
    logged = log_store.by_slot_for_tasks(
        session, [t.id for t in candidates], {"checkin_sent", *log_store.RESOLUTION_KINDS}
    )
    for task in candidates:
        slot = booked_slot(task)
        if slot is None or slot[1] + CHECKIN_DELAY > now:
            continue
        if logged.get((task.id, task.scheduled_date)):
            continue
        if now - slot[1] > CHECKIN_MAX_AGE:
            stale.append(task)
            continue
        snapshot = await features_snapshot(session, task, slot[0], slot[1], now=now)
        log_store.add(
            session, task_id=task.id, kind="checkin_sent", actor="system",
            slot_start=task.scheduled_date, slot_end=slot[1], features=snapshot,
        )
        session.commit()
        await notify_checkin(task, start=slot[0], end=slot[1])
        sent += 1
    return sent, stale


async def _settle_unanswered(session: Session, now: datetime, stale: list) -> int:
    settled = 0
    for row in log_store.pending_checkins(session, sent_before=now - NO_RESPONSE_AFTER):
        task = tasks_store.get(session, row.task_id)
        if task is None or tasks_store.latest_status_for(session, [task.id]).get(task.id, "open") != "open":
            log_store.add(session, task_id=row.task_id, kind="no_response", actor="system",
                          slot_start=row.slot_start, slot_end=row.slot_end)
            session.commit()
            continue
        await record_missed(session, task, kind="no_response", actor="system",
                            slot_start=row.slot_start, slot_end=row.slot_end)
        settled += 1
    for task in stale:
        slot = booked_slot(task)
        await record_missed(session, task, kind="no_response", actor="system",
                            slot_start=task.scheduled_date, slot_end=slot[1] if slot else None)
        settled += 1
    return settled


async def record_missed(
    session: Session, task, *, kind: str, actor: str, slot_start, slot_end
) -> None:
    """Not done / no answer: log, deduct the slot penalty silently, re-plan."""
    from app.events import publish_points, publish_task
    from app.services.notify import clear_task_notification
    from app.services.plan.schedule import schedule_task
    from app.services.points import subtract_scheduled_overdue_penalty

    log_store.add(session, task_id=task.id, kind=kind, actor=actor,
                  slot_start=slot_start, slot_end=slot_end)
    session.commit()
    if slot_start is not None and subtract_scheduled_overdue_penalty(
        session, task, scheduled_date=slot_start
    ):
        publish_points(session)
    await clear_task_notification(task.id)
    result = await schedule_task(session, task, notify=False)
    if result is not None:
        publish_task(session, task.id)


# Last end written per running task, so an unchanged extension isn't
# re-patched every sweep.
_extended_to: dict[uuid.UUID, datetime] = {}


async def _extend_running(session: Session, now: datetime) -> tuple[int, int]:
    extended = dropped = 0
    for task in tasks_store.running_open(session):
        outcome = await _extend(session, task, now)
        if outcome == "extended":
            extended += 1
        elif outcome == "abandoned":
            dropped += 1
    return extended, dropped


async def _extend(session: Session, task, now: datetime) -> str | None:
    """Grow a running task's event to the current step, or end the run once
    it's abandoned. Returns "extended", "abandoned", or None when unchanged."""
    from app.events import publish_task
    from app.services.calendar import update_task_event
    from app.services.plan.schedule import schedule_task
    from app.services.plan.snapshot import calendar_items

    slot = booked_slot(task)
    if slot is None:
        return None
    start, booked_end = slot
    if abandoned(start, booked_end, now):
        log_store.add(session, task_id=task.id, kind="abandoned", actor="system",
                      slot_start=task.scheduled_date,
                      slot_end=running_end(start, booked_end, now, task.running_since))
        task.running_since = None
        session.commit()
        _extended_to.pop(task.id, None)
        await schedule_task(session, task, notify=False)
        publish_task(session, task.id)
        return "abandoned"
    target = running_end(start, booked_end, now, task.running_since)
    try:
        events = await calendar_items(session, booked_end, target)
    except Exception as exc:  # noqa: BLE001
        log.warning("checkin · extension context failed task=%s err=%s", task.id, exc)
        events = []
    # Events never move: stop short of the next one, the clock keeps running.
    blocking = [e.start for e in events if booked_end <= e.start < target]
    if blocking:
        target = max(booked_end, min(blocking))
    if _extended_to.get(task.id) == target:
        return None
    try:
        await update_task_event(session, task, start=start, end=target)
    except Exception as exc:  # noqa: BLE001
        log.warning("checkin · extension write failed task=%s err=%s", task.id, exc)
        return None
    _extended_to[task.id] = target
    await _clear_way(session, task, booked_end, target)
    return "extended"


async def _clear_way(session: Session, running, start: datetime, end: datetime) -> None:
    """Re-plan a movable task only once the extension actually reaches it."""
    from app.services.plan.replan import batch
    from app.services.plan.schedule import Interval, schedule_task

    # One solve for everything the extension reaches, not one per task.
    async with batch(session):
        for other in tasks_store.open_scheduled_between(
            session, time_min=start - timedelta(hours=12), time_max=end, exclude_task_id=running.id
        ):
            if other.running_since is not None:
                continue
            slot = booked_slot(other)
            if slot is None or not preempted(slot, [(start, end)]):
                continue
            await schedule_task(session, other, block=Interval(start, end), notify=False)


# --- Actions -----------------------------------------------------------------------


async def handle_checkin_action(session: Session, task, action: str) -> None:
    """Apply a check-in answer to the slot the check-in asked about. A late
    answer to a check-in that was already settled (re-planned after no
    answer) only closes the task on Done."""
    from app.events import publish_task
    from app.services.notify import clear_task_notification
    from app.services.task.close import close_task

    if action not in CHECKIN_ACTIONS:
        raise ValueError(f"unknown check-in action: {action}")
    pending = _pending_checkin(session, task)
    if pending is None:
        if action == ACTION_CHECKIN_DONE:
            await close_task(session, task.id)
        else:
            await clear_task_notification(task.id)
        return
    slot_start, slot_end, snapshot = pending.slot_start, pending.slot_end, pending.features
    if action == ACTION_CHECKIN_DONE:
        log_store.add(session, task_id=task.id, kind="done_confirmed", actor="checkin",
                      slot_start=slot_start, slot_end=slot_end, features=snapshot)
        session.commit()
        await close_task(session, task.id)
    elif action == ACTION_CHECKIN_STILL_WORKING:
        log_store.add(session, task_id=task.id, kind="still_working", actor="checkin",
                      slot_start=slot_start, slot_end=slot_end, features=snapshot)
        now = datetime.now(timezone.utc)
        task.running_since = now
        session.commit()
        await clear_task_notification(task.id)
        # The first step lands now; the sweep adds one every step after.
        await _extend(session, task, now)
        publish_task(session, task.id)
    else:
        await record_missed(session, task, kind="not_done", actor="checkin",
                            slot_start=slot_start, slot_end=slot_end)
    from app.services.learn.jobs import refit_soon

    refit_soon()


def _pending_checkin(session: Session, task):
    sent = log_store.latest_of_kind(session, task.id, "checkin_sent")
    if sent is None:
        return None
    kinds = {row.kind for row in log_store.for_slot(session, task.id, sent.slot_start)}
    if kinds & (log_store.RESOLUTION_KINDS | {"closed"}):
        return None
    return sent
