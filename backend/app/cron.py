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
from app.services.input.poll import poll_sources
from app.services.plan import refresh_commutes_for_weather, schedule_task, scheduled_interval_for

log = logging.getLogger(__name__)

AUTO_POLL_INTERVAL_S = 300
DISCOVER_INTERVAL_S = 300
OVERDUE_TASK_INTERVAL_S = 300
WEATHER_INTERVAL_S = int(timedelta(hours=1).total_seconds())
OVERDUE_TASK_GRACE = timedelta(minutes=15)


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
                "calendar-discover done · checked=%d updated=%d overlapping=%d",
                summary["checked"],
                summary["updated"],
                summary["overlapping"],
            )
        except asyncio.CancelledError:
            log.info("calendar-discover loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("calendar-discover iteration failed")


async def reschedule_overdue_scheduled_tasks_once() -> dict[str, int]:
    cutoff = datetime.now(timezone.utc) - OVERDUE_TASK_GRACE
    attempted = 0
    rescheduled = 0
    with SessionLocal() as session:
        overdue = tasks_store.overdue_scheduled_open(session, cutoff=cutoff)
        for task in overdue:
            block = scheduled_interval_for(task)
            attempted += 1
            result = await schedule_task(session, task, block=block)
            if result is None:
                continue
            rescheduled += 1
            publish_task(session, task.id)
    return {"attempted": attempted, "rescheduled": rescheduled}


async def _overdue_scheduled_tasks() -> None:
    log.info("overdue-scheduled-tasks loop started · interval=%ds", OVERDUE_TASK_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(OVERDUE_TASK_INTERVAL_S)
            if not state.auto_poll_enabled:
                log.debug("overdue-scheduled-tasks skipped (disabled)")
                continue
            summary = await reschedule_overdue_scheduled_tasks_once()
            log.info(
                "overdue-scheduled-tasks done · attempted=%d rescheduled=%d",
                summary["attempted"],
                summary["rescheduled"],
            )
        except asyncio.CancelledError:
            log.info("overdue-scheduled-tasks loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("overdue-scheduled-tasks iteration failed")


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
        except asyncio.CancelledError:
            log.info("weather-commute-refresh loop cancelled")
            raise
        except Exception:  # noqa: BLE001 — best-effort background loop
            log.exception("weather-commute-refresh iteration failed")


_JOBS: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
    ("auto-poll", _auto_poll),
    ("calendar-discover", _calendar_discover),
    ("overdue-scheduled-tasks", _overdue_scheduled_tasks),
    ("weather-commute-refresh", _weather_commute_refresh),
]

# Names of jobs that only make sense with commute enabled.
_COMMUTE_JOBS = {"weather-commute-refresh"}

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
