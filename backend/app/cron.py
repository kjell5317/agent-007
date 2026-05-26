"""Scheduled / background jobs.

Single entry point for the FastAPI lifespan to start and stop every
timed automation. Add a new periodic job by writing an `async def`
coroutine and appending it to `_JOBS` below — `start` creates one
asyncio task per job, `stop` cancels them all.

Today the only job is the source-poll loop, which drives
`poll_sources` on a fixed interval, gated by `state.auto_poll_enabled`
so the UI can pause polling without restarting the process. One
iteration's failure never kills the loop — exceptions are logged and
the loop continues.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from app import state
from app.config import get_settings
from app.db import SessionLocal
from app.services.calendar.discover import discover_updated_events
from app.services.input.poll import poll_sources

log = logging.getLogger(__name__)

AUTO_POLL_INTERVAL_S = 300
DISCOVER_INTERVAL_S = 300


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


_JOBS: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
    ("auto-poll", _auto_poll),
    ("calendar-discover", _calendar_discover),
]

_tasks: list[asyncio.Task] = []


async def start() -> None:
    if _tasks:
        return
    for name, runner in _JOBS:
        _tasks.append(asyncio.create_task(runner(), name=name))
    log.info("cron jobs started · %s", ", ".join(name for name, _ in _JOBS))


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
