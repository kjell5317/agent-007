"""Scheduled / background jobs.

Single entry point for the FastAPI lifespan to start and stop every
timed automation. Add a new periodic job by writing an `async def`
coroutine and appending it to `_JOBS` below — `start` creates one
asyncio task per job, `stop` cancels them all.

Jobs are gated by `state.auto_poll_enabled` so the UI can pause background
automation without restarting the process. One iteration's failure never kills
the loop — exceptions are logged and the loop continues.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta, timezone
from typing import Any

from app import state
from app.config import get_settings
from app.db import SessionLocal
from app.db.clients import tasks as tasks_store
from app.events import publish_points, publish_task
from app.services.calendar.discover import discover_updated_events
from app.services.input.poll import poll_sources
from app.services.notify import notify_points_penalty, notify_task_created
from app.services.plan import (
    cleanup_stray_commute_legs,
    refresh_commutes_for_weather,
    schedule_task,
    scheduled_interval_for,
)
from app.services.points import (
    PENALTY_POINTS,
    subtract_due_overdue_penalties,
    subtract_scheduled_overdue_penalty,
)

log = logging.getLogger(__name__)

AUTO_POLL_INTERVAL_S = 300
# Every minute: calendar-discover is a cheap incremental syncToken pull (only
# what changed since the last poll), so it can run tight without hammering the
# Google API — and a near-real-time reschedule around external edits is worth it.
DISCOVER_INTERVAL_S = 60
OVERDUE_TASK_INTERVAL_S = 300
OVERDUE_DUE_INTERVAL_S = 300
WEATHER_INTERVAL_S = int(timedelta(hours=1).total_seconds())
OVERDUE_TASK_GRACE = timedelta(minutes=15)
COMMUTE_CLEANUP_INTERVAL_S = int(timedelta(hours=1).total_seconds())
# A task the planner could not place stays overdue — without a backoff the
# 5-minute loop would re-run the whole (expensive) displacement search
# forever. One retry per hour is plenty; any calendar edit or task update
# triggers its own reschedule anyway.
NO_SLOT_RETRY = timedelta(hours=1)
_no_slot_until: dict[Any, datetime] = {}


async def _auto_poll() -> None:
    log.info("auto-poll loop started · interval=%ds", AUTO_POLL_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(AUTO_POLL_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("auto-poll skipped (disabled)")
                continue
            with SessionLocal() as session:
                summary = await poll_sources(session)
            log.info(
                "auto-poll done · fetched=%d created=%d skipped=%d errors=%d",
                summary["fetched"],
                summary["tasks_created"],
                summary["skipped"],
                len(summary["errors"]),
            )
        except asyncio.CancelledError:
            log.info("auto-poll loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("auto-poll iteration failed")


async def _calendar_discover() -> None:
    """Sweep the user's calendars for externally-edited events and trigger a
    reschedule when an edit produced an overlap. Gated by the same toggle as
    auto-poll so the UI's pause button stops both."""
    log.info("calendar-discover loop started · interval=%ds", DISCOVER_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(DISCOVER_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("calendar-discover skipped (disabled)")
                continue
            settings = get_settings()
            write_id = (settings.google_calendar_id or "").strip()
            if not write_id:
                log.debug("calendar-discover skipped · no google_calendar_id configured")
                continue
            ids = [write_id, *settings.google_busy_calendar_ids]
            with SessionLocal() as session:
                summary = await discover_updated_events(session, calendar_ids=ids)
            log.info(
                "calendar-discover done · checked=%d updated=%d overlapping=%d"
                " synced=%d deleted=%d cached=%d",
                summary["checked"],
                summary["updated"],
                summary["overlapping"],
                summary["scheduled_updates"],
                summary["deleted"],
                summary.get("cached", 0),
            )
            if summary["updated"] or summary["deleted"]:
                retry = await retry_unscheduled_tasks_once(respect_backoff=False)
                if retry["attempted"]:
                    log.info(
                        "calendar-discover retry · attempted=%d scheduled=%d",
                        retry["attempted"],
                        retry["scheduled"],
                    )
        except asyncio.CancelledError:
            log.info("calendar-discover loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("calendar-discover iteration failed")


async def reschedule_overdue_scheduled_tasks_once() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    cutoff = now - OVERDUE_TASK_GRACE
    attempted = 0
    rescheduled = 0
    points_subtracted = 0
    with SessionLocal() as session:
        overdue = tasks_store.overdue_scheduled_open(session, cutoff=cutoff)
        for task in overdue:
            scheduled_date = task.scheduled_date
            block = scheduled_interval_for(task)
            if block is None or block.end + OVERDUE_TASK_GRACE > now:
                continue
            backoff = _no_slot_until.get(task.id)
            if backoff is not None and now < backoff:
                continue
            attempted += 1
            result = await schedule_task(session, task, block=block)
            # The slot was missed whether or not a new one was found — the
            # penalty applies either way (idempotent per slot). On failure
            # the stale slot was cleared and the retry sweep takes over.
            if subtract_scheduled_overdue_penalty(
                session,
                task,
                scheduled_date=scheduled_date,
            ):
                points_subtracted += PENALTY_POINTS
                publish_points(session)
                await notify_points_penalty(
                    task,
                    points=PENALTY_POINTS,
                    reason="scheduled date was overdue",
                )
            if result is None:
                _no_slot_until[task.id] = now + NO_SLOT_RETRY
                continue
            _no_slot_until.pop(task.id, None)
            rescheduled += 1
            publish_task(session, task.id)
    return {
        "attempted": attempted,
        "rescheduled": rescheduled,
        "points_subtracted": points_subtracted,
    }


async def retry_unscheduled_tasks_once(*, respect_backoff: bool = True) -> dict[str, int]:
    """Try to place open due-dated tasks that currently have no slot (their
    stale slot was cleared after a failed schedule). Failures stay silent —
    the original "could not schedule" notification stands until either a
    retry succeeds or the user intervenes. `respect_backoff=False` is for
    discover-triggered sweeps: the calendar actually changed, so waiting
    out the hourly backoff would sit on fresh room."""
    now = datetime.now(timezone.utc)
    attempted = 0
    scheduled = 0
    with SessionLocal() as session:
        for task in tasks_store.open_unscheduled_due(session):
            backoff = _no_slot_until.get(task.id)
            if respect_backoff and backoff is not None and now < backoff:
                continue
            attempted += 1
            result = await schedule_task(session, task, notify=False)
            if result is None:
                _no_slot_until[task.id] = now + NO_SLOT_RETRY
                continue
            _no_slot_until.pop(task.id, None)
            scheduled += 1
            await notify_task_created(task, start=result[0], end=result[1])
            publish_task(session, task.id)
    return {"attempted": attempted, "scheduled": scheduled}


async def penalize_overdue_due_tasks_once() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    checked = 0
    penalized = 0
    points_subtracted = 0
    with SessionLocal() as session:
        overdue = tasks_store.overdue_due_open(session, cutoff=now)
        for task in overdue:
            checked += 1
            amount = subtract_due_overdue_penalties(session, task, now=now)
            if amount <= 0:
                continue
            penalized += 1
            points_subtracted += amount
            await notify_points_penalty(
                task,
                points=amount,
                reason="task is past due",
            )
        if points_subtracted:
            publish_points(session)
    return {
        "checked": checked,
        "penalized": penalized,
        "points_subtracted": points_subtracted,
    }


async def _overdue_scheduled_tasks() -> None:
    log.info("overdue-scheduled-tasks loop started · interval=%ds", OVERDUE_TASK_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(OVERDUE_TASK_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("overdue-scheduled-tasks skipped (disabled)")
                continue
            summary = await reschedule_overdue_scheduled_tasks_once()
            retry = await retry_unscheduled_tasks_once()
            log.info(
                "overdue-scheduled-tasks done · attempted=%d rescheduled=%d points=%d"
                " retry_attempted=%d retry_scheduled=%d",
                summary["attempted"],
                summary["rescheduled"],
                summary["points_subtracted"],
                retry["attempted"],
                retry["scheduled"],
            )
        except asyncio.CancelledError:
            log.info("overdue-scheduled-tasks loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("overdue-scheduled-tasks iteration failed")


async def _overdue_due_tasks() -> None:
    log.info("overdue-due-tasks loop started · interval=%ds", OVERDUE_DUE_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(OVERDUE_DUE_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("overdue-due-tasks skipped (disabled)")
                continue
            summary = await penalize_overdue_due_tasks_once()
            log.info(
                "overdue-due-tasks done · checked=%d penalized=%d points=%d",
                summary["checked"],
                summary["penalized"],
                summary["points_subtracted"],
            )
        except asyncio.CancelledError:
            log.info("overdue-due-tasks loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("overdue-due-tasks iteration failed")


async def _weather_commute_refresh() -> None:
    """Refresh existing commute events when hourly weather changes mode choice."""
    log.info("weather-commute-refresh loop started · interval=%ds", WEATHER_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(WEATHER_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("weather-commute-refresh skipped (disabled)")
                continue
            with SessionLocal() as session:
                summary = await refresh_commutes_for_weather(session)
            log.info(
                "weather-commute-refresh done · planned=%d rescheduled_tasks=%d errors=%d",
                summary["planned"],
                summary["rescheduled_tasks"],
                len(summary["errors"]),
            )
            if summary["errors"]:
                log.warning("weather-commute-refresh errors · %s", summary["errors"])
        except asyncio.CancelledError:
            log.info("weather-commute-refresh loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("weather-commute-refresh iteration failed")


async def _commute_cleanup() -> None:
    """Periodic sweep deleting commute legs outside the live window — already
    ended or beyond the lookahead horizon. calendar-discover replans around
    every changed/visible event, so no separate startup migration is needed."""
    log.info("commute-cleanup loop started · interval=%ds", COMMUTE_CLEANUP_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(COMMUTE_CLEANUP_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("commute-cleanup skipped (disabled)")
                continue
            with SessionLocal() as session:
                deleted = await cleanup_stray_commute_legs(session)
            log.info("commute-cleanup done · deleted=%d", deleted)
        except asyncio.CancelledError:
            log.info("commute-cleanup loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("commute-cleanup iteration failed")


_JOBS: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
    ("auto-poll", _auto_poll),
    ("calendar-discover", _calendar_discover),
    ("overdue-scheduled-tasks", _overdue_scheduled_tasks),
    ("overdue-due-tasks", _overdue_due_tasks),
    ("weather-commute-refresh", _weather_commute_refresh),
    ("commute-cleanup", _commute_cleanup),
]

# Names of jobs that only make sense with commute enabled.
_COMMUTE_JOBS = {"weather-commute-refresh", "commute-cleanup"}

_tasks: list[asyncio.Task] = []


async def start() -> None:
    if _tasks:
        return
    settings = get_settings()
    jobs = [
        (name, runner)
        for name, runner in _JOBS
        if settings.commute_enabled or name not in _COMMUTE_JOBS
    ]
    for name, runner in jobs:
        _tasks.append(asyncio.create_task(runner(), name=name))
    log.info("cron jobs started · %s", ", ".join(name for name, _ in jobs))


async def stop() -> None:
    if not _tasks:
        return
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _tasks.clear()
    log.info("cron jobs stopped")
