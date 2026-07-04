"""Task update scheduling trigger logic."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services.location import resolve_location_alias

log = logging.getLogger(__name__)

PLAN_TRIGGER_FIELDS: frozenset[str] = frozenset({"estimation", "due_date", "location"})


async def update_task_to_calendar(
    session: Session,
    task,
    *,
    changed_fields: Iterable[str] | None = None,
) -> None:
    """Patch or reschedule a task after a plan-relevant edit."""
    changed = set(changed_fields or ())
    if not changed & PLAN_TRIGGER_FIELDS:
        return

    if "location" in changed:
        # A location edit changes the trip block around the task, so the slot
        # must be re-planned. When the location was removed, the placement
        # no longer replans commutes itself — sweep the vacated window so
        # the orphaned legs are dropped and the old neighbours reconnect.
        from app.services.plan.schedule import schedule_task, scheduled_interval_for

        prior = scheduled_interval_for(task)
        await schedule_task(session, task)
        if not resolve_location_alias(task.location) and prior is not None:
            await _replan_window(session, prior.start, prior.end)
        return

    current = await _current_event(session, task)
    if current is None:
        from app.services.plan.schedule import schedule_task

        await schedule_task(session, task)
        return

    should_reschedule = await _patch_requires_reschedule(session, task, current, changed)
    if should_reschedule:
        from app.services.plan.schedule import schedule_task

        await schedule_task(session, task)
        return

    from app.services.calendar import update_task_event

    duration = current.end - current.start
    if "estimation" in changed:
        duration = _duration_for(task)
    from app.services.plan.schedule import task_event_span_on_grid

    start, end = task_event_span_on_grid(current.start, duration)
    await update_task_event(
        session,
        task,
        start=start,
        end=end,
        changed_fields=changed or None,
    )

    # Moving or resizing a located task leaves its legs anchored to the old
    # span — re-derive them around the touched window.
    if resolve_location_alias(task.location) and (start != current.start or end != current.end):
        await _replan_window(
            session,
            min(current.start, start),
            max(current.end, end),
        )


async def _replan_window(session: Session, start, end) -> None:
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    margin = commute_window_margin()
    await plan_commutes_window_best_effort(
        session,
        window_start=start - margin,
        window_end=end + margin,
    )


async def _patch_requires_reschedule(session: Session, task, current, changed: set[str]) -> bool:
    if "estimation" not in changed and "due_date" not in changed:
        return False

    if task.due_date is None:
        return True

    duration = current.end - current.start
    if "estimation" in changed:
        duration = _duration_for(task)
    from app.services.plan.schedule import task_event_span_on_grid

    candidate_start, candidate_end = task_event_span_on_grid(current.start, duration)
    if candidate_end > task.due_date:
        return True

    return await _overlaps_other_event(session, task, candidate_start, candidate_end)


async def _overlaps_other_event(session: Session, task, start, end) -> bool:
    from app.services.calendar import is_commute_event, list_events_between

    settings = get_settings()
    ids = _busy_calendar_ids(settings)
    if not ids:
        return False
    commute_gap = timedelta(minutes=settings.commute_event_buffer_minutes)
    event_gap = timedelta(minutes=settings.event_buffer_minutes)
    pad = max(commute_gap, event_gap)
    events = await list_events_between(
        session, calendar_ids=ids, time_min=start - pad, time_max=end + pad,
    )
    for ev in events:
        if ev.all_day or ev.id == task.calendar_event_id:
            continue
        gap = commute_gap if is_commute_event(ev) else event_gap
        if ev.start < end + gap and start - gap < ev.end:
            return True
    return False


def _duration_for(task) -> timedelta:
    return timedelta(
        minutes=max(5, int(task.estimation or get_settings().google_calendar_default_event_minutes))
    )


async def _current_event(session: Session, task):
    if not task.calendar_event_id:
        return None
    from app.services.calendar import get_event

    calendar_id = (get_settings().google_calendar_id or "").strip()
    if not calendar_id:
        return None
    try:
        return await get_event(session, calendar_id=calendar_id, event_id=task.calendar_event_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.update · current event lookup failed task=%s err=%s", task.id, exc)
        return None


def _busy_calendar_ids(settings) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for cid in [settings.google_calendar_id, *settings.google_busy_calendar_ids]:
        clean = (cid or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out
