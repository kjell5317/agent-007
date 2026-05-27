"""Task scheduling on the calendar.

This service is the sole funnel into `services.calendar` for slot-
dependent work, and the sole caller of `services.commute`. Task updates,
agent flows, manual source polling, and daily commute cron all route through
here instead of touching calendar or commute directly.

Public surface:

  * `add_task_to_calendar(session, task)`     — plan + create the mirror.
  * `update_task_to_calendar(session, task,   — re-plan + patch the mirror.
     *, changed_fields=None)`
  * `plan_commutes(session, *, account_key=None)` — week-ahead commute plan.

Plus the lower-level slot planner (still wired even while the service
is paused — `services.commute` uses it internally):

  * `plan_task_slot(session, task)` — pick `(start, end)` for one task.
  * `plan_tasks(session, tasks)`    — batch variant, urgent-first.

The one explicit exception to "only plan touches calendar": when a task
update changes no plan-relevant field (estimation / due_date / location),
the task service may call `calendar.update_task_event` directly so a
plain rename doesn't run the planner. The corresponding exception for
delete: `close` / `dismiss` go straight to
`calendar.delete_task_event` since no planning is involved.
"""

from app.services.plan.commute import plan_commutes, plan_commutes_window
from app.services.plan.schedule import (
    Interval,
    plan_task_slot,
    plan_tasks,
    schedule,
)
from app.services.plan.reschedule import reschedule
from app.services.plan.update import update_task_to_calendar

__all__ = [
    "Interval",
    "plan_commutes",
    "plan_commutes_window",
    "plan_task_slot",
    "plan_tasks",
    "schedule",
    "reschedule",
    "update_task_to_calendar",
]
