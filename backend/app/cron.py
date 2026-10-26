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
from app.events import publish_task
from app.services.calendar.discover import discover_updated_events
from app.services.calendar.labels import sync as sync_labels
from app.services.input.poll import poll_sources
from app.services.plan import cleanup_stray_commute_legs, refresh_commutes_for_weather
from app.services.plan.checkin import checkin_overdue_slots_once
from app.services.plan.schedule import announce_placement
from app.services.task.recur import plan_recurring_tasks

log = logging.getLogger(__name__)

AUTO_POLL_INTERVAL_S = 300
# Every minute: calendar-discover is a cheap incremental syncToken pull (only
# what changed since the last poll), so it can run tight without hammering the
# Google API — and a near-real-time reschedule around external edits is worth it.
DISCOVER_INTERVAL_S = 60
CHECKIN_SWEEP_INTERVAL_S = 300
WEATHER_INTERVAL_S = int(timedelta(hours=1).total_seconds())
NIGHTLY_CHECK_INTERVAL_S = 600
COMMUTE_CLEANUP_INTERVAL_S = int(timedelta(hours=1).total_seconds())
# Repeat instances are materialized a week ahead, so hourly keeps the window
# rolling without daily ones ever lagging.
RECURRING_TASKS_INTERVAL_S = int(timedelta(hours=1).total_seconds())
# Labels only change when the user edits them in Calendar, and the settings
# page syncs on open — this loop just keeps the mirror from going stale (and
# populates it on a fresh deploy, before anyone opens that page).
LABEL_SYNC_INTERVAL_S = int(timedelta(hours=6).total_seconds())
LABEL_SYNC_STARTUP_DELAY_S = 20
# Tasks the solver could not place stay unplaced — without a backoff the
# 5-minute loop would re-run a full solve for them forever. One retry per hour
# is plenty; any calendar edit or task update triggers its own solve anyway.
NO_SLOT_RETRY = timedelta(hours=1)
_no_slot_until: datetime | None = None


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
            # Only changes the app didn't make itself free or take room; a retry
            # on our own writes would re-solve after every solve.
            if summary.get("foreign") or summary["deleted"] or summary["scheduled_updates"]:
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


async def retry_unscheduled_tasks_once(*, respect_backoff: bool = True) -> dict[str, int]:
    """One solve for the open due-dated tasks that currently have no slot
    (their stale slot was cleared after a failed placement). Failures stay
    silent — the original "could not schedule" notification stands until
    either a retry succeeds or the user intervenes. `respect_backoff=False` is
    for discover-triggered sweeps: the calendar actually changed, so waiting
    out the hourly backoff would sit on fresh room."""
    from app.services.plan.replan import request_replan

    global _no_slot_until
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        # Deferred tasks wait on purpose; every solve reconsiders them.
        pending = [
            task for task in tasks_store.open_unscheduled_due(session)
            if task.schedule_state != "deferred"
        ]
        if not pending:
            return {"attempted": 0, "scheduled": 0}
        if respect_backoff and _no_slot_until is not None and now < _no_slot_until:
            return {"attempted": 0, "scheduled": 0}
        result = await request_replan(session, reason="retry_unplaced")
        scheduled = 0
        for task in pending:
            session.refresh(task)
            span = result.placed.get(task.id)
            if task.schedule_state == "placed" and span is not None:
                scheduled += 1
                await announce_placement(session, task, start=span[0], end=span[1])
                publish_task(session, task.id)
        _no_slot_until = now + NO_SLOT_RETRY if scheduled < len(pending) else None
    return {"attempted": len(pending), "scheduled": scheduled}


async def _checkin_sweep() -> None:
    log.info("checkin-sweep loop started · interval=%ds", CHECKIN_SWEEP_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(CHECKIN_SWEEP_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("checkin-sweep skipped (disabled)")
                continue
            summary = await checkin_overdue_slots_once()
            retry = await retry_unscheduled_tasks_once()
            log.info(
                "check-in sweep done · sent=%d no_response=%d extended=%d abandoned=%d"
                " retry_attempted=%d retry_scheduled=%d",
                summary["sent"],
                summary["no_response"],
                summary["extended"],
                summary["abandoned"],
                retry["attempted"],
                retry["scheduled"],
            )
        except asyncio.CancelledError:
            log.info("checkin-sweep loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("checkin-sweep iteration failed")


async def _weather_commute_refresh() -> None:
    """Re-solve when the hourly forecast moved enough to change mode choices."""
    log.info("weather-commute-refresh loop started · interval=%ds", WEATHER_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(WEATHER_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("weather-commute-refresh skipped (disabled)")
                continue
            with SessionLocal() as session:
                legs = await refresh_commutes_for_weather(session)
            log.info("weather-commute-refresh done · %s", "no re-solve" if legs is None else f"legs={legs}")
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


async def _recurring_tasks() -> None:
    log.info("recurring-tasks loop started · interval=%ds", RECURRING_TASKS_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(RECURRING_TASKS_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("recurring-tasks skipped (disabled)")
                continue
            with SessionLocal() as session:
                created = await plan_recurring_tasks(session)
            log.info("recurring-tasks done · created=%d", len(created))
        except asyncio.CancelledError:
            log.info("recurring-tasks loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("recurring-tasks iteration failed")


async def nightly_scheduler_once() -> None:
    """Refit the learned models, then give the solver its long run: 60 s
    instead of the per-trigger 10 s, so the gap left during the day closes."""
    from app.services.learn.jobs import nightly
    from app.services.plan.replan import request_replan

    from app.services.calendar.attributes import classify_pending_events
    from app.timezones import user_tz

    settings = get_settings()
    with SessionLocal() as session:
        await nightly(session)
        try:
            # Events the discovery pass didn't get to (its classifier only runs
            # on passes that cached something).
            await classify_pending_events(
                session,
                until=datetime.now(user_tz()) + timedelta(days=settings.scheduler_horizon_days),
            )
            session.commit()
        except Exception:  # noqa: BLE001
            log.exception("nightly-scheduler · event classification failed")
            session.rollback()
        await request_replan(
            session, reason="nightly", time_limit=settings.scheduler_nightly_time_limit_seconds,
        )


async def _nightly_scheduler() -> None:
    log.info("nightly-scheduler loop started · hour=%d", get_settings().scheduler_nightly_hour)
    last_run: str | None = None
    while True:
        try:
            await asyncio.sleep(NIGHTLY_CHECK_INTERVAL_S)
            if not state.auto_poll_enabled:
                continue
            from app.timezones import user_tz

            local = datetime.now(user_tz())
            if local.hour != get_settings().scheduler_nightly_hour or last_run == local.date().isoformat():
                continue
            last_run = local.date().isoformat()
            await nightly_scheduler_once()
            log.info("nightly-scheduler done")
        except asyncio.CancelledError:
            log.info("nightly-scheduler loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("nightly-scheduler iteration failed")


async def _label_sync() -> None:
    """Mirror the calendar's event labels, starting shortly after boot."""
    log.info("label-sync loop started · interval=%ds", LABEL_SYNC_INTERVAL_S)
    delay = LABEL_SYNC_STARTUP_DELAY_S
    while True:
        try:
            await asyncio.sleep(delay)
            delay = LABEL_SYNC_INTERVAL_S
            if not state.auto_poll_enabled:
                log.debug("label-sync skipped (disabled)")
                continue
            with SessionLocal() as session:
                labels = await sync_labels(session)
            log.info("label-sync done · labels=%d", len(labels))
        except asyncio.CancelledError:
            log.info("label-sync loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("label-sync iteration failed")


_JOBS: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
    ("auto-poll", _auto_poll),
    ("label-sync", _label_sync),
    ("calendar-discover", _calendar_discover),
    ("checkin-sweep", _checkin_sweep),
    ("weather-commute-refresh", _weather_commute_refresh),
    ("commute-cleanup", _commute_cleanup),
    ("recurring-tasks", _recurring_tasks),
    ("nightly-scheduler", _nightly_scheduler),
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
