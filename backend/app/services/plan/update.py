"""Re-plan a task and push the new slot to its calendar mirror.

Called by `services.task.update` when a patch changes a plan-relevant
field (due_date, estimation, location). The plain "user renamed the
task" case bypasses this module and calls
`calendar.update_task_event` directly without start/end — that's the
explicit exception to the "only plan touches calendar" rule.
"""

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
    """Re-plan `task` and patch its calendar event with the new slot.
    """
    changed = set(changed_fields or ())
    plan_changed = changed & PLAN_TRIGGER_FIELDS
    if not plan_changed:
        log.debug(
            "plan.update_task_to_calendar · no plan fields changed task=%s changed=%s",
            task.id,
            sorted(changed),
        )
        return
    if task.due_date is None:
        log.debug("plan.update_task_to_calendar · task=%s has no due_date", task.id)
        return

    from app.services.calendar import get_event, update_task_event
    from app.services.plan.schedule import notify_no_slot, plan_task_slot

    try:
        start, end = await plan_task_slot(session, task)
    except ValueError:
        await notify_no_slot(task)
        return

    old_start = None
    old_end = None
    if task.calendar_event_id:
        calendar_id = (get_settings().google_calendar_id or "").strip()
        if calendar_id:
            try:
                current = await get_event(
                    session,
                    calendar_id=calendar_id,
                    event_id=task.calendar_event_id,
                )
                old_start = current.start
                old_end = current.end
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "plan.update_task_to_calendar · current event lookup failed task=%s err=%s",
                    task.id,
                    exc,
                )

    await update_task_event(
        session,
        task,
        start=start,
        end=end,
        changed_fields=changed or None,
    )

    if await _should_replan_commutes(session, task, start, end, plan_changed):
        from app.services.plan.commute import plan_commutes_window_best_effort

        event_ids = {task.calendar_event_id} if task.calendar_event_id else None
        if (
            event_ids
            and old_start is not None
            and old_end is not None
            and not _windows_touch(old_start, old_end, start, end)
        ):
            old_window_start, old_window_end = _commute_window(old_start, old_end)
            await plan_commutes_window_best_effort(
                session,
                window_start=old_window_start,
                window_end=old_window_end,
                target_event_ids=set(),
                stale_event_ids=event_ids,
            )

        target_event_ids = (
            {task.calendar_event_id}
            if task.calendar_event_id and (getattr(task, "location", None) or "").strip()
            else None
        )
        window_start, window_end = _commute_window(start, end)
        await plan_commutes_window_best_effort(
            session,
            window_start=window_start,
            window_end=window_end,
            target_event_ids=target_event_ids,
            stale_event_ids=event_ids,
        )


async def _should_replan_commutes(
    session: Session,
    task,
    start,
    end,
    plan_changed: set[str],
) -> bool:
    # Location changes need a commute refresh even when the new value is empty:
    # old commute legs may need to be removed.
    if "location" in plan_changed:
        return True
    if (getattr(task, "location", None) or "").strip():
        return True
    return await _is_close_to_other_event(session, task, start, end)


async def _is_close_to_other_event(session: Session, task, start, end) -> bool:
    from app.services.calendar import is_commute_event
    from app.services.calendar.client import authorized_client, normalize

    settings = get_settings()
    calendar_ids = _busy_calendar_ids(settings)
    if not calendar_ids:
        return False

    margin = timedelta(
        minutes=max(
            settings.commute_bike_max_minutes,
            settings.commute_event_buffer_minutes,
        )
    )
    window_start = start - margin
    window_end = end + margin
    current_event_id = getattr(task, "calendar_event_id", None)

    client = await authorized_client(session, None)
    for calendar_id in calendar_ids:
        items = await client.list_events(
            calendar_id,
            time_min=window_start,
            time_max=window_end,
        )
        for raw in items:
            if raw.get("status") == "cancelled" or raw.get("transparency") == "transparent":
                continue
            ev = normalize(raw, calendar_id)
            if ev.all_day or ev.id == current_event_id or is_commute_event(ev):
                continue
            if window_start < ev.end and ev.start < window_end:
                return True
    return False


def _busy_calendar_ids(settings) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for cid in [settings.google_calendar_id, *settings.google_busy_calendar_ids]:
        clean = (cid or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _commute_window(start, end) -> tuple:
    margin = _commute_margin()
    return start - margin, end + margin


def _windows_touch(old_start, old_end, new_start, new_end) -> bool:
    margin = _commute_margin()
    return old_start - margin < new_end + margin and new_start - margin < old_end + margin


def _commute_margin() -> timedelta:
    settings = get_settings()
    return timedelta(
        minutes=max(
            settings.commute_bike_max_minutes,
            settings.commute_home_layover_minutes * 2,
            settings.commute_event_buffer_minutes,
        )
    )
