"""Repeating tasks: set or remove a series' rule and materialize its instances.

A series is every task sharing `recurrence_series_id`, each carrying the same
rule. Like commute legs, instances are only created a week ahead — plus the
next one whenever nothing open lies ahead, so a monthly task always has its
next occurrence planned. An occurrence still open past its due date holds the
series: nothing new is created until it is closed, and missed dates are
skipped. Every instance is a plain task anchored by its own
`recurrence` input, so it closes, scores and reschedules like any other.
Splitting one only splits that occurrence: new instances are never copied
as split, and its subtasks stay inside the occurrence's period.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import tasks as tasks_store
from app.db.models.raw_input import RawInput
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate
from app.events import publish_task, publish_task_removed
from app.services.calendar import delete_task_event
from app.services.notify import clear_task_notification
from app.services.plan import schedule_task
from app.services.plan.replan import batch as replan_batch
from app.services.task.split import inherited_attributes
from app.services.recurrence import occurrences_to_create, validate
from app.services.task.vacate import replan_vacated_window, vacated_commute_window
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)


def set_recurrence(
    session: Session, task_id: uuid.UUID, unit: str, interval: int
) -> tuple[Task, list[uuid.UUID]]:
    """Apply the rule to the task's series (starting one if needed), anchored
    on this task. Returns the task plus the open instances after it that the
    old rule generated — the caller drops them so the new rule can replan."""
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    if task.parent_task_id:
        raise ValueError("Subtasks can't repeat — set it on the parent task")
    validate(unit, interval)
    if (task.recurrence_unit, task.recurrence_interval) == (unit, interval):
        return task, []

    stale = _open_after(session, task) if task.recurrence_series_id else []
    if task.recurrence_series_id is None:
        task.recurrence_series_id = uuid.uuid4()
        session.flush()
    tasks_store.set_series_recurrence(
        session, task.recurrence_series_id, unit=unit, interval=interval, anchor=task.due_date,
    )
    session.commit()
    _publish_series(session, task.recurrence_series_id)
    return task, stale


def remove_recurrence(session: Session, task_id: uuid.UUID) -> tuple[Task, list[uuid.UUID]]:
    """End the series: every instance stops repeating. Returns the open
    instances after this task, for the caller to drop."""
    task = tasks_store.get(session, task_id)
    if task is None:
        raise LookupError("Task not found")
    series_id = task.recurrence_series_id
    if series_id is None:
        return task, []

    stale = _open_after(session, task)
    members = [member.id for member in tasks_store.series(session, series_id)]
    tasks_store.clear_series_recurrence(session, series_id)
    session.commit()
    for member_id in members:
        if member_id not in stale:
            publish_task(session, member_id)
    return task, stale


async def drop_instances(session: Session, task_ids: Iterable[uuid.UUID]) -> None:
    """Delete generated instances along with their calendar mirrors. One the
    user completed or split in the meantime is kept."""
    for task_id in task_ids:
        task = tasks_store.get(session, task_id)
        if task is None or task.is_container:
            continue
        if tasks_store.latest_status_for(session, [task_id]).get(task_id, "open") != "open":
            continue
        vacated = vacated_commute_window(task)
        await delete_task_event(session, task)
        await clear_task_notification(task_id)
        tasks_store.delete_with_inputs(session, task)
        session.commit()
        publish_task_removed(task_id)
        await replan_vacated_window(session, vacated)


async def plan_recurring_tasks(
    session: Session, series_ids: Iterable[uuid.UUID] | None = None
) -> list[Task]:
    """Create the instances each series (default: all) is missing, then
    place them on the calendar."""
    created = create_due_instances(session, datetime.now(user_tz()), series_ids)
    async with replan_batch(session):
        for task in created:
            try:
                await schedule_task(session, task, notify=False)
            except Exception:  # noqa: BLE001 — the instance stays; the retry sweep places it
                log.exception("repeat instance scheduling failed · task=%s", task.id)
    for task in created:
        publish_task(session, task.id)
    return created


def create_due_instances(
    session: Session, now: datetime, series_ids: Iterable[uuid.UUID] | None = None
) -> list[Task]:
    created: list[Task] = []
    ids = list(series_ids) if series_ids is not None else tasks_store.recurring_series_ids(session)
    for series_id in ids:
        instances = tasks_store.series(session, series_id)
        if not instances or instances[-1].recurrence_unit is None:
            continue
        latest = instances[-1]
        statuses = tasks_store.latest_status_for(session, [t.id for t in instances])
        open_dues = [
            to_user_tz(t.due_date) for t in instances if statuses.get(t.id, "open") == "open"
        ]
        # The late occurrence is still the work to do; the next would land in
        # the same days it is being rescheduled into. Closing it replans the
        # series at once.
        if any(due <= now for due in open_dues):
            continue
        for due in occurrences_to_create(
            to_user_tz(latest.recurrence_anchor),
            latest.recurrence_unit,
            latest.recurrence_interval,
            latest_due=to_user_tz(latest.due_date),
            now=now,
            has_upcoming=bool(open_dues),
        ):
            created.append(_create_instance(session, latest, due))
    session.commit()
    return created


def _create_instance(session: Session, template: Task, due: datetime) -> Task:
    task = tasks_store.create(session, TaskCreate(
        title=template.title,
        description=template.description,
        link=template.link,
        due_date=due,
        estimation=template.estimation,
        location=template.location,
        label=template.label,
        recurrence_unit=template.recurrence_unit,
        recurrence_interval=template.recurrence_interval,
        recurrence_anchor=template.recurrence_anchor,
        recurrence_series_id=template.recurrence_series_id,
        **inherited_attributes(template),
    ))
    session.add(RawInput(
        source="recurrence",
        content=task.title,
        status="open",
        task_id=task.id,
        source_metadata={
            "series_id": str(template.recurrence_series_id),
            "previous_task_id": str(template.id),
        },
        processed_at=datetime.now(timezone.utc),
    ))
    return task


def _open_after(session: Session, task: Task) -> list[uuid.UUID]:
    # A split occurrence carries the user's own subtasks; it survives a rule
    # change like a completed one instead of being regenerated.
    later = [
        member for member in tasks_store.series(session, task.recurrence_series_id)
        if member.id != task.id and member.due_date > task.due_date and not member.is_container
    ]
    statuses = tasks_store.latest_status_for(session, [member.id for member in later])
    return [member.id for member in later if statuses.get(member.id, "open") == "open"]


def _publish_series(session: Session, series_id: uuid.UUID) -> None:
    for member in tasks_store.series(session, series_id):
        publish_task(session, member.id)
