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

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


async def update_task_to_calendar(
    session: Session,
    task,
    *,
    changed_fields: Iterable[str] | None = None,
) -> None:
    """Re-plan `task` and patch its calendar event with the new slot.

    Disabled — see the plan service's module docstring. When re-enabled,
    pick a fresh slot via `plan_task_slot` and forward `start` / `end` +
    `changed_fields` to `calendar.update_task_event`.
    """
    log.debug(
        "plan.update_task_to_calendar · disabled (task=%s changed=%s)",
        getattr(task, "id", "?"),
        sorted(changed_fields) if changed_fields else (),
    )
    return None
