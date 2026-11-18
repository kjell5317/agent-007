"""Start cues, slots that pass unstarted, and running work sessions.

When a slot begins, one push asks to start the task — Start / In 30 min /
Not today. The cue and what follows it are the follow-through signal the
learner trains on:

* Start — a work session begins (`services.task.work`), logged `started`.
* In 30 min — the task moves at least 30 minutes on, logged `cue_snoozed`.
* Not today — the task moves to tomorrow or later, logged `cue_declined`.
* Nothing — once the slot is over the next solve re-plans the task; the
  learner reads a cue without any of the above as "not started". No points
  are deducted for a missed slot, only for a missed hard deadline.

A session that outlasts its slot grows the calendar event in 15-minute steps,
stopping short of the next event; at 2× the booked length (and at least 90
minutes past it) it is taken for forgotten and ends as `abandoned`.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import schedule_log as log_store
from app.db.clients import tasks as tasks_store
from app.services.notifications import messages
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

# A cue still goes out this long after the slot began (a sweep that ran late).
CUE_WINDOW = timedelta(minutes=10)
# A slot counts as missed this long after it ended — time to tap Start late.
MISSED_GRACE = timedelta(minutes=15)
SNOOZE = timedelta(minutes=30)
EXTENSION_STEP = timedelta(minutes=15)
ABANDON_FACTOR = 2
# A resumed task books only what's left, so twice that can be minutes: a
# session is taken for forgotten only this far past its booked end at least.
ABANDON_GRACE = timedelta(minutes=90)
# A hard deadline that passed longer ago than this costs no points: it passed
# before this rule existed, or while the service was down.
DEADLINE_PENALTY_WINDOW = timedelta(days=1)

CUE_ACTIONS = frozenset({messages.ACTION_SNOOZE_START, messages.ACTION_NOT_TODAY})


# --- Pure timing helpers (shared with the solver input) -----------------------


def booked_slot(task) -> tuple[datetime, datetime] | None:
    from app.services.plan.schedule import _duration_minutes

    if task.scheduled_date is None:
        return None
    start = to_user_tz(task.scheduled_date)
    return start, start + timedelta(minutes=_duration_minutes(task, get_settings()))


def abandon_point(start: datetime, booked_end: datetime) -> datetime:
    booked = booked_end - start
    return start + max(ABANDON_FACTOR * booked, booked + ABANDON_GRACE)


def running_end(start: datetime, booked_end: datetime, now: datetime, since: datetime) -> datetime:
    """Where a running session's event ends at `now`: the next 15-minute step
    past now, counted from the session start at `since`, capped at the
    abandon point and never short of the booked end."""
    steps = math.floor(max(now - since, timedelta(0)) / EXTENSION_STEP) + 1
    return max(booked_end, min(abandon_point(start, booked_end), since + steps * EXTENSION_STEP))


def abandoned(start: datetime, booked_end: datetime, now: datetime) -> bool:
    return now >= abandon_point(start, booked_end)


def preempted(slot: tuple[datetime, datetime], overtime: list[tuple[datetime, datetime]]) -> bool:
    """A slot that a running task's overtime grew into didn't happen — the
    user was still on the running task — so it must move even once its time
    has begun or passed."""
    return any(lo < slot[1] and slot[0] < hi for lo, hi in overtime)


def cue_due(slot: tuple[datetime, datetime], now: datetime) -> bool:
    return slot[0] <= now < min(slot[1], slot[0] + CUE_WINDOW)


def missed(slot: tuple[datetime, datetime], now: datetime) -> bool:
    return slot[1] + MISSED_GRACE <= now


def end_of_day(now: datetime) -> datetime:
    local = now.astimezone(user_tz())
    return datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=user_tz())


def cue_key(task_id: uuid.UUID, slot_start: datetime) -> str:
    return f"cue:{task_id}:{slot_start.astimezone(timezone.utc).isoformat()}"


def cue_current(task, key: str, now: datetime) -> bool:
    """The cue under `key` still asks about the task's booked slot, which
    hasn't ended and isn't being worked on."""
    if task is None or task.running_since is not None or task.schedule_state != "placed":
        return False
    slot = booked_slot(task)
    return slot is not None and cue_key(task.id, slot[0]) == key and now < slot[1]


# --- Sweep -------------------------------------------------------------------------


async def sweep_once() -> dict[str, int]:
    from app.db import SessionLocal

    with SessionLocal() as session:
        now = datetime.now(user_tz())
        # Overruns first: a slot a running session grows into moves before
        # its cue would go out.
        summary = {}
        summary["extended"], summary["abandoned"] = await _extend_running(session, now)
        summary["stale_cues"] = await _clear_stale_cues(session, now)
        summary["cued"] = await _send_cues(session, now)
        summary["missed"] = await _replan_missed(session, now)
        summary["deadline_penalties"] = _penalize_missed_deadlines(session, now)
    return summary


async def _send_cues(session: Session, now: datetime) -> int:
    from app.services.notifications.notify import send_once
    from app.services.plan.snapshot import features_snapshot

    sent = 0
    for task in tasks_store.open_scheduled_between(
        session, time_min=now - CUE_WINDOW, time_max=now + timedelta(seconds=1)
    ):
        if task.is_container or task.running_since is not None or not task.calendar_event_id:
            continue
        if task.schedule_state != "placed":
            continue
        slot = booked_slot(task)
        if slot is None or not cue_due(slot, now):
            continue
        if not await send_once(
            session, key=cue_key(task.id, slot[0]), kind="start_cue",
            message=messages.start_cue(task, start=slot[0], end=slot[1]), task_id=task.id,
        ):
            continue
        snapshot = await features_snapshot(session, task, slot[0], slot[1], now=now)
        log_store.add(
            session, task_id=task.id, kind="cue_sent", actor="system",
            slot_start=task.scheduled_date, slot_end=slot[1], features=snapshot,
        )
        session.commit()
        sent += 1
    return sent


async def _clear_stale_cues(session: Session, now: datetime) -> int:
    """A cue whose slot moved, ended or got started goes away."""
    from app.db.clients import notification_ledger as ledger
    from app.services.notifications import push

    cleared = 0
    for row in ledger.showing_of_kind(session, "start_cue"):
        task = tasks_store.get(session, row.task_id) if row.task_id else None
        if cue_current(task, row.key, now):
            continue
        await push.clear(row.tag)
        ledger.record_cleared(session, row, datetime.now(timezone.utc))
        cleared += 1
    if cleared:
        session.commit()
    return cleared


async def _replan_missed(session: Session, now: datetime) -> int:
    """Any solve moves a slot that passed unstarted; this makes sure one runs."""
    from app.services.plan.replan import request_replan

    count = 0
    for task in tasks_store.overdue_scheduled_open(session, cutoff=now):
        if task.running_since is not None or task.schedule_state != "placed":
            continue
        slot = booked_slot(task)
        if slot is not None and missed(slot, now):
            count += 1
    if count:
        await request_replan(session, reason="missed_slot")
    return count


async def _extend_running(session: Session, now: datetime) -> tuple[int, int]:
    extended = dropped = 0
    for task in tasks_store.running_open(session):
        outcome = await _extend(session, task, now)
        if outcome == "extended":
            extended += 1
        elif outcome == "abandoned":
            dropped += 1
    return extended, dropped


# Last end written per running task, so an unchanged extension isn't
# re-patched every sweep.
_extended_to: dict[uuid.UUID, datetime] = {}


async def _extend(session: Session, task, now: datetime) -> str | None:
    """Grow a running task's event to the current step, or end the session
    once it's abandoned. Returns "extended", "abandoned", or None."""
    from app.services.calendar import update_task_event
    from app.services.plan.snapshot import calendar_items

    slot = booked_slot(task)
    if slot is None:
        return None
    start, booked_end = slot
    if abandoned(start, booked_end, now):
        from app.services.task.work import end_abandoned

        _extended_to.pop(task.id, None)
        await end_abandoned(session, task, at=abandon_point(start, booked_end))
        return "abandoned"
    target = running_end(start, booked_end, now, to_user_tz(task.running_since))
    try:
        events = await calendar_items(session, booked_end, target)
    except Exception as exc:  # noqa: BLE001
        log.warning("cue · extension context failed task=%s err=%s", task.id, exc)
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
        log.warning("cue · extension write failed task=%s err=%s", task.id, exc)
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
            await schedule_task(session, other, block=Interval(start, end))


def _penalize_missed_deadlines(session: Session, now: datetime) -> int:
    from app.events import publish_points
    from app.services.points import subtract_missed_deadline_penalty

    count = 0
    for task in tasks_store.open_schedulable(session):
        if task.deadline_kind != "hard" or task.due_date is None:
            continue
        due = to_user_tz(task.due_date)
        if not now - DEADLINE_PENALTY_WINDOW <= due <= now:
            continue
        if subtract_missed_deadline_penalty(session, task):
            count += 1
    if count:
        publish_points(session)
    return count


# --- Actions -----------------------------------------------------------------------


async def handle_cue_action(session: Session, task, action: str, *, now: datetime | None = None) -> None:
    """In 30 min / Not today on the start cue: move the task off the slot the
    cue was about, at least that far."""
    from app.events import publish_task
    from app.services.notifications.notify import clear_task_notifications
    from app.services.plan.schedule import Interval, schedule_task

    from app.db.clients import notification_ledger as ledger

    if action not in CUE_ACTIONS:
        raise ValueError(f"unknown cue action: {action}")
    now = now or datetime.now(user_tz())
    slot = booked_slot(task) if task.schedule_state == "placed" else None
    cued = ledger.get(session, cue_key(task.id, slot[0])) if slot is not None else None
    current = cued is not None and cued.cleared_at is None and cue_current(task, cued.key, now)
    await clear_task_notifications(session, task.id, kinds={"start_cue"})
    # A tap on a cue whose slot already moved on must not count against the new one.
    if not current:
        return
    snoozed = action == messages.ACTION_SNOOZE_START
    log_store.add(
        session, task_id=task.id, kind="cue_snoozed" if snoozed else "cue_declined",
        actor="user", slot_start=task.scheduled_date, slot_end=slot[1],
    )
    session.commit()
    until = now + SNOOZE if snoozed else end_of_day(now)
    if await schedule_task(session, task, block=Interval(min(slot[0], now), until)) is not None:
        publish_task(session, task.id)
    from app.services.learn.jobs import refit_soon

    refit_soon()
