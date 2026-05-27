"""Repair managed write-calendar events that collide with hard blockers."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models.task import Task
from app.services.calendar import (
    get_event,
    is_commute_event,
    is_managed_event,
    private_properties,
    update_task_event,
)
from app.services.calendar.client import authorized_client, normalize
from app.services.plan.schedule import notify_no_slot, plan_task_slot

log = logging.getLogger(__name__)

MAX_RECURSION_DEPTH = 8


async def reschedule(
    session: Session,
    *,
    event_id: str,
    account_key: str | None = None,
) -> None:
    """Move a managed write-calendar event and recursively repair fallout."""
    await _reschedule_one(
        session,
        event_id=event_id,
        account_key=account_key,
        visited=set(),
        depth=0,
    )


async def _reschedule_one(
    session: Session,
    *,
    event_id: str,
    account_key: str | None,
    visited: set[str],
    depth: int,
) -> None:
    if event_id in visited:
        return
    if depth > MAX_RECURSION_DEPTH:
        log.warning("plan.reschedule · recursion limit hit event=%s", event_id)
        return
    visited.add(event_id)

    task = _task_for_event(session, event_id)
    if task is not None:
        await _reschedule_task(
            session,
            task,
            account_key=account_key,
            visited=visited,
            depth=depth,
        )
        return

    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return

    try:
        event = await get_event(
            session,
            calendar_id=calendar_id,
            event_id=event_id,
            account_key=account_key,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plan.reschedule · get event failed event=%s err=%s", event_id, exc)
        return

    if is_commute_event(event):
        from app.services.plan.commute import plan_commutes_window_best_effort

        related_event_id = private_properties(event).get("related_event_id")
        await plan_commutes_window_best_effort(
            session,
            window_start=event.start,
            window_end=event.end,
            target_event_ids={related_event_id} if related_event_id else None,
            stale_event_ids={related_event_id} if related_event_id else None,
            account_key=account_key,
        )
        return

    if is_managed_event(event):
        log.info("plan.reschedule · managed non-task event ignored event=%s", event_id)
    else:
        log.debug("plan.reschedule · unmanaged write event ignored event=%s", event_id)


async def _reschedule_task(
    session: Session,
    task: Task,
    *,
    account_key: str | None,
    visited: set[str],
    depth: int,
) -> None:
    if task.due_date is None or not task.calendar_event_id:
        log.debug("plan.reschedule · task=%s cannot be planned", task.id)
        return

    try:
        start, end = await plan_task_slot(session, task, account_key=account_key)
    except ValueError:
        log.warning("plan.reschedule · no slot for task=%s", task.id)
        await notify_no_slot(task)
        return

    old_start = None
    old_end = None
    calendar_id = (get_settings().google_calendar_id or "").strip()
    if calendar_id:
        try:
            current = await get_event(
                session,
                calendar_id=calendar_id,
                event_id=task.calendar_event_id,
                account_key=account_key,
            )
            old_start = current.start
            old_end = current.end
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "plan.reschedule · current event lookup failed task=%s err=%s",
                task.id,
                exc,
            )

    await update_task_event(session, task, start=start, end=end)
    from app.services.plan.commute import plan_commutes_window_best_effort

    margin = _commute_window_margin()
    event_ids = {task.calendar_event_id}
    if (
        old_start is not None
        and old_end is not None
        and not _windows_touch(old_start, old_end, start, end, margin)
    ):
        await plan_commutes_window_best_effort(
            session,
            window_start=old_start - margin,
            window_end=old_end + margin,
            target_event_ids=set(),
            stale_event_ids=event_ids,
            account_key=account_key,
        )
    await plan_commutes_window_best_effort(
        session,
        window_start=start - margin,
        window_end=end + margin,
        target_event_ids=event_ids if (task.location or "").strip() else None,
        stale_event_ids=event_ids,
        account_key=account_key,
    )
    log.info(
        "plan.reschedule · task=%s moved to %s..%s",
        task.id,
        start.isoformat(),
        end.isoformat(),
    )

    for overlap_id in await _managed_overlaps(
        session,
        event_id=task.calendar_event_id,
        start=start,
        end=end,
        account_key=account_key,
    ):
        await _reschedule_one(
            session,
            event_id=overlap_id,
            account_key=account_key,
            visited=visited,
            depth=depth + 1,
        )


async def _managed_overlaps(
    session: Session,
    *,
    event_id: str,
    start,
    end,
    account_key: str | None,
) -> list[str]:
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    if not calendar_id:
        return []

    client = await authorized_client(session, account_key)
    items = await client.list_events(calendar_id, time_min=start, time_max=end)
    out: list[str] = []
    for item in items:
        if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
            continue
        ev = normalize(item, calendar_id)
        if ev.id == event_id or ev.all_day or not is_managed_event(ev):
            continue
        if start < ev.end and ev.start < end:
            out.append(ev.id)
    return out


def _task_for_event(session: Session, event_id: str) -> Task | None:
    stmt = select(Task).where(Task.calendar_event_id == event_id)
    return session.execute(stmt).scalar_one_or_none()


def _commute_window_margin():
    from datetime import timedelta

    settings = get_settings()
    return timedelta(
        minutes=max(
            settings.commute_bike_max_minutes,
            settings.commute_home_layover_minutes * 2,
            settings.commute_event_buffer_minutes,
        )
    )


def _windows_touch(old_start, old_end, new_start, new_end, margin) -> bool:
    return old_start - margin < new_end + margin and new_start - margin < old_end + margin
