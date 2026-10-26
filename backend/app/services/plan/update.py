"""Task update scheduling trigger logic."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


async def update_task_to_calendar(
    session: Session,
    task,
    *,
    changed_fields: Iterable[str] | None = None,
) -> None:
    """Any plan-relevant edit is a new solve; the solver decides whether the
    slot moves. Non-time event fields are patched first."""
    from app.services.calendar import update_task_event
    from app.services.plan.commute import replan_commutes
    from app.services.plan.schedule import schedule_task, scheduled_interval_for

    changed = set(changed_fields or ())
    prior = scheduled_interval_for(task)
    # The solve leaves a slot whose start holds untouched, so a new length is
    # written here — else the next calendar sync reads the stale end as a resize.
    resized = prior if "estimation" in changed and prior is not None else None
    if task.calendar_event_id and (resized or changed & {
        "title", "description", "link", "location", "label", "due_date", "deadline_kind",
    }):
        await update_task_event(
            session, task, changed_fields=changed,
            start=resized.start if resized else None, end=resized.end if resized else None,
        )
    await schedule_task(session, task)
    if "location" in changed and prior is not None:
        await replan_commutes(session)
