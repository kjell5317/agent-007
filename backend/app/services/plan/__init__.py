"""Task scheduling on the calendar.

This service is the sole funnel into `services.calendar` for slot-
dependent work, and the sole caller of `services.commute`. Task updates,
agent flows, the manual `POST /commute/plan` trigger — they all route
through here instead of touching calendar or commute directly.

Public surface:

  * `add_task_to_calendar(session, task)`     — plan + create the mirror.
  * `update_task_to_calendar(session, task,   — re-plan + patch the mirror.
     *, changed_fields=None)`
  * `plan_commutes(session, *, account_key=None)` — week-ahead commute plan.

Plus the lower-level slot planner (still wired even while the service
is paused — `services.commute` uses it internally):

  * `plan_task_slot(session, task)` — pick `(start, end)` for one task.
  * `plan_tasks(session, tasks)`    — batch variant, urgent-first.

**Currently disabled.** Each of the three public funnels is a no-op
that logs at debug — the architecture is in place so callers can
already route through here, but slot planning and calendar mirroring
are paused until the planner is re-enabled. Flip them back on by
restoring their original bodies (see the per-module docstrings).

The one explicit exception to "only plan touches calendar": when a task
update changes no plan-relevant field (estimation / due_date / location),
the task service may call `calendar.update_task_event` directly so a
plain rename doesn't run the planner. The corresponding exception for
delete: `close` / `dismiss` go straight to
`calendar.delete_task_event` since no planning is involved.
"""

from app.services.plan.schedule import (
    Interval,
    schedule,
)
from app.services.plan.reschedule import reschedule

__all__ = [
    "Interval",
    "schedule",
    "reschedule",
]
