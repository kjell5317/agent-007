"""Task scheduling on the calendar.

This service is the sole funnel into `services.calendar` for slot-
dependent work, and the sole caller of `services.commute`. Task updates,
agent flows, discover repairs, and hourly weather refreshes all route through
here instead of touching calendar or commute directly.

Public surface:

  * `schedule_task(session, task, ...)` — place a task with a solve, which
    writes its calendar mirror and the commute legs.
  * `reschedule_event(session, event_id, ...)` — discover dispatcher: task
    event → schedule_task; commute event → a fresh solve.
  * `update_task_to_calendar(session, task, *, changed_fields=None)`
  * `refresh_commutes_for_weather(session)` — hourly weather-driven re-solve.

The one explicit exception to "only plan touches calendar": when a task
update changes no plan-relevant field, the task service may call
`calendar.update_task_event` directly so a plain rename doesn't run the
solver. The corresponding exception for delete: `close` / `dismiss` go
straight to `calendar.delete_task_event` since no planning is involved.
"""

from app.services.plan.commute import (
    cleanup_stray_commute_legs,
    refresh_commutes_for_weather,
    replan_commutes,
)
from app.services.plan.schedule import (
    Interval,
    reschedule_event,
    schedule_task,
    scheduled_interval_for,
)
from app.services.plan.update import update_task_to_calendar

__all__ = [
    "Interval",
    "cleanup_stray_commute_legs",
    "refresh_commutes_for_weather",
    "replan_commutes",
    "reschedule_event",
    "schedule_task",
    "scheduled_interval_for",
    "update_task_to_calendar",
]
