"""Task update scheduling trigger logic."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import get_settings

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
    if task.due_date is None:
        log.debug("plan.update · task=%s has no due_date", task.id)
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

    end = current.end
    if "estimation" in changed:
        end = current.start + timedelta(
            minutes=max(5, int(task.estimation or get_settings().google_calendar_default_event_minutes))
        )
    await update_task_event(
        session,
        task,
        start=current.start,
        end=end,
        changed_fields=changed or None,
    )

    if "location" in changed:
        from app.services.plan.commute import (
            commute_window_margin,
            plan_commutes_window_best_effort,
        )

        margin = commute_window_margin()
        event_ids = {task.calendar_event_id} if task.calendar_event_id else None
        await plan_commutes_window_best_effort(
            session,
            window_start=current.start - margin,
            window_end=end + margin,
            target_event_ids=event_ids if (task.location or "").strip() else None,
            stale_event_ids=event_ids,
        )


async def _patch_requires_reschedule(session: Session, task, current, changed: set[str]) -> bool:
    if current.end > task.due_date:
        return True
    if "estimation" not in changed and "due_date" not in changed:
        return False

    duration = timedelta(
        minutes=max(5, int(task.estimation or get_settings().google_calendar_default_event_minutes))
    )
    candidate_start = current.start
    candidate_end = candidate_start + duration
    if candidate_end > task.due_date:
        return True

    buffer = timedelta(minutes=get_settings().commute_event_buffer_minutes)
    return await _overlaps_other_event(
        session,
        task,
        candidate_start - buffer,
        candidate_end + buffer,
    )


async def _overlaps_other_event(session: Session, task, start, end) -> bool:
    from app.services.calendar import list_events_between

    settings = get_settings()
    ids = _busy_calendar_ids(settings)
    if not ids:
        return False
    events = await list_events_between(session, calendar_ids=ids, time_min=start, time_max=end)
    for ev in events:
        if ev.all_day or ev.id == task.calendar_event_id:
            continue
        if ev.start < end and start < ev.end:
            return True
    return False


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
