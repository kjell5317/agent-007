"""The "Reschedule" tap: move a task off its slot, or plan a task waiting in
"Later" this week."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.db.clients import schedule_log as log_store
from app.db.models.task import Task
from app.services.plan.schedule import schedule_task, scheduled_interval_for
from app.services.plan.snapshot import features_snapshot


async def reschedule_task(session: Session, task: Task) -> tuple[datetime, datetime] | None:
    if task.schedule_state == "deferred":
        return await schedule_task(session, task, pull_in=True)
    block = scheduled_interval_for(task)
    if block is not None and task.calendar_event_id:
        from app.services.learn.jobs import refit_soon

        # The slot's outcome for the preference model: it wasn't going to happen there.
        log_store.add(
            session, task_id=task.id, kind="rescheduled", actor="user",
            slot_start=block.start, slot_end=block.end,
            features=await features_snapshot(session, task, block.start, block.end),
        )
        session.commit()
        refit_soon()
    return await schedule_task(session, task, block=block)
