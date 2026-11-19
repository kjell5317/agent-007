"""The shopping task, where groceries hand over to the task side. One open
task at a time: created, or brought forward, when something runs out; placed
by the scheduler with the store as its location; closed when the last list
entry is crossed off. It's created like a repeat instance — directly, anchored
by its own generated input — not through the extraction agent."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import grocery as grocery_store
from app.db.clients import tasks as tasks_store
from app.db.models.raw_input import RawInput
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate
from app.events import publish_task
from app.services.plan import schedule_task
from app.services.task.close import close_task
from app.services.task.update import update_task
from app.timezones import to_user_tz, user_tz

log = logging.getLogger(__name__)

TITLE = "Grocery shopping"
ESTIMATE_MINUTES = 45
# A shop-by time already past still leaves room to go.
MIN_LEAD = timedelta(hours=1)


def due_for(shop_by: date, at: time, now: datetime) -> datetime:
    """`at` is the store's shop-by time of day (a grocery setting)."""
    due = datetime.combine(shop_by, at, tzinfo=user_tz())
    return max(due, now + MIN_LEAD)


def open_task(session: Session) -> Task | None:
    task_id = grocery_store.settings(session).shopping_task_id
    if task_id is None:
        return None
    task = tasks_store.get(session, task_id)
    if task is None:
        return None
    status = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    return task if status == "open" else None


async def ensure_task(session: Session, shop_by: date) -> None:
    """A shopping task due on `shop_by`: a new one, or the open one moved
    earlier. Never later — something else already needs the earlier trip."""
    now = datetime.now(timezone.utc)
    due = due_for(shop_by, grocery_store.settings(session).shop_by_time, now)
    task = open_task(session)
    if task is None:
        await _create(session, due, now)
    elif due < task.due_date:
        await update_task(session, task.id, {"due_date": due})


async def close_if_done(session: Session) -> None:
    """Crossing off the last entry is the trip done."""
    if grocery_store.open_items(session):
        return
    task = open_task(session)
    if task is not None:
        await close_task(session, task.id)


async def sync_store(session: Session) -> None:
    """The open task follows the store settings: its location, and its due
    time of day on the same day."""
    task = open_task(session)
    if task is None:
        return
    settings = grocery_store.settings(session)
    due = due_for(
        to_user_tz(task.due_date).date(), settings.shop_by_time, datetime.now(timezone.utc)
    )
    fields: dict[str, object] = {}
    if task.location != settings.store_location:
        fields["location"] = settings.store_location
    if due != task.due_date:
        fields["due_date"] = due
    if fields:
        await update_task(session, task.id, fields)


async def _create(session: Session, due: datetime, now: datetime) -> None:
    settings = grocery_store.settings(session)
    task = tasks_store.create(session, TaskCreate(
        title=TITLE,
        due_date=due,
        estimation=ESTIMATE_MINUTES,
        location=settings.store_location,
        link=f"{get_settings().public_url}/#grocery",
    ))
    session.add(RawInput(
        source="grocery",
        content=TITLE,
        status="open",
        task_id=task.id,
        source_metadata={},
        processed_at=now,
    ))
    settings.shopping_task_id = task.id
    session.commit()
    try:
        await schedule_task(session, task)
    except Exception:  # noqa: BLE001 — the task stays; the retry sweep places it
        log.exception("shopping task scheduling failed · task=%s", task.id)
    publish_task(session, task.id)
    log.info("shopping task created · task=%s due=%s", task.id, due.isoformat())
