"""Reconnect commute legs after a located task leaves the calendar.

Deleting a task's mirror cascades its own legs (see
`calendar.delete_task_event`), but the neighbours it was chained between
still need fresh legs — capture the window before the delete, replan it
after."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.services.location import resolve_location_alias
from app.services.plan.schedule import Interval, scheduled_interval_for


def vacated_commute_window(task) -> Interval | None:
    if not resolve_location_alias(task.location):
        return None
    return scheduled_interval_for(task)


async def replan_vacated_window(session: Session, vacated: Interval | None) -> None:
    if vacated is None:
        return
    from app.services.plan.commute import commute_window_margin, plan_commutes_window_best_effort

    margin = commute_window_margin()
    await plan_commutes_window_best_effort(
        session,
        window_start=vacated.start - margin,
        window_end=vacated.end + margin,
    )
