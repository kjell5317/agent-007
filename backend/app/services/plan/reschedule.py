"""Re-plan a calendar event whose slot was invalidated.

Placeholder. Called by `services.calendar.discover` when it detects an
externally-edited event that now overlaps with another event in the
window.

The eventual implementation should:
  1. Find the task linked to `calendar_id` + `event_id` (if any).
  2. Recompute its slot via `services.plan.schedule.plan_task_slot`,
     treating the overlapping event as a busy block.
  3. Patch the calendar event to the new slot.
  4. Recurse into any other events that the new slot now displaces.

For now this is a no-op so callers can wire the hand-off without
blocking on the algorithm.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


async def reschedule(
    session: Session,
    *,
    event_id: str,
    account_key: str | None = None,
) -> None:
    log.info(
        "TODO reschedule_event · calendar=%s event=%s — not implemented yet",
        event_id,
    )
