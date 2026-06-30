"""Task scheduling on the calendar.

This service is the sole funnel into `services.calendar` for slot-
dependent work, and the sole caller of `services.commute`. Task updates,
agent flows, discover repairs, and hourly weather refreshes all route through
here instead of touching calendar or commute directly.

Public surface:

  * `schedule_task(session, task, ...)` — plan + create/update the mirror.
  * `reschedule_event(session, event_id, ...)` — discover dispatcher: task
    event → schedule_task; commute event → recompute commute plan.
  * `update_task_to_calendar(session, task, *, changed_fields=None)`
  * `refresh_commutes_for_weather(session)` — hourly weather-driven refresh.
  * `plan_task_slot(session, task)` — pick `(start, end)` for one task.

The one explicit exception to "only plan touches calendar": when a task
update changes no plan-relevant field (estimation / due_date / location),
the task service may call `calendar.update_task_event` directly so a
plain rename doesn't run the planner. The corresponding exception for
delete: `close` / `dismiss` go straight to
`calendar.delete_task_event` since no planning is involved.
"""

from app.services.plan.commute import (
    refresh_commutes_for_weather,
)
from app.services.plan.schedule import (
    Interval,
    plan_task_slot,
    reschedule_event,
    schedule_task,
    scheduled_interval_for,
)
from app.services.plan.update import update_task_to_calendar

__all__ = [
    "Interval",
    "refresh_commutes_for_weather",
    "plan_task_slot",
    "reschedule_event",
    "schedule_task",
    "scheduled_interval_for",
    "update_task_to_calendar",
]
