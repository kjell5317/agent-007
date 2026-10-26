"""Single choke point for emitting SSE events.

Every mutation site calls one of these after it commits. They build the exact
same read DTOs the REST endpoints return, serialize once, and hand the JSON to
the bus. Keeping all serialization here (rather than at each call site) is what
makes the "full payload push" safe: there is one place that can drift, not ten.

Frontend contract — every event is `{"type": ..., ...}`:
  * {"type": "task", "data": <TaskRead>}     — upsert; drop from the open list
                                               when data.status != "open"
  * {"type": "task_removed", "id": <uuid>}   — task row deleted (dismiss / orphan)
  * {"type": "input", "data": <RawInputRead>} — upsert into the inbox
  * {"type": "points", "total": <float>}     — new points total (topbar display)
  * {"type": "kotx"}                         — a kotx run changed; the client
                                               refetches /kotx/tasks (the run
                                               list lives upstream, so there is
                                               no payload to push)
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy.orm import Session

from app.db.clients import (
    points as points_store,
    raw_inputs as raw_inputs_store,
    tasks as tasks_store,
)
from app.db.schemas.raw_input import RawInputRead
from app.db.schemas.task import SubtaskSummary, TaskRawInputRead, TaskRead
from app.events import bus
from app.services.source_url import source_url_for_raw_input


def _emit(event: dict) -> None:
    # Imported here: the search package imports the calendar services, which
    # publish through this module.
    from app.services.search.suggest import invalidate as invalidate_suggestions

    # Whatever changed may now match a search differently.
    invalidate_suggestions()
    bus.publish(json.dumps(event))


def task_read(session: Session, task, status_: str, is_manual: bool) -> TaskRead:
    raw = raw_inputs_store.latest_for_task(session, task.id)
    linked_inputs = raw_inputs_store.list_for_task(session, task.id)
    parent_card = None
    if parent_id := getattr(task, "parent_task_id", None):
        # The parent's original input is the source of each derived subtask.
        parent_inputs = raw_inputs_store.list_for_task(session, parent_id)
        linked_inputs.extend(parent_inputs)
        parent = tasks_store.get(session, parent_id)
        if parent is not None:
            parent_status = next((item.status for item in parent_inputs if item.status != "duplicate"), "open")
            parent_manual = bool(parent_inputs) and all(item.source in {"manual", "web_research"} for item in parent_inputs)
            parent_card = TaskRead.build(parent, parent_status, parent_manual)
    children = tasks_store.children(session, task.id) if getattr(task, "is_container", False) else []
    child_statuses = tasks_store.latest_status_for(session, [child.id for child in children])
    return TaskRead.build(
        task,
        status_,
        is_manual,
        parent_card=parent_card,
        source_url=source_url_for_raw_input(raw),
        raw_inputs=[
            TaskRawInputRead.build(
                linked,
                source_url=source_url_for_raw_input(linked),
            )
            for linked in linked_inputs
        ],
        subtasks=[SubtaskSummary(
            id=child.id, public_id=child.public_id, title=child.title, due_date=child.due_date,
            estimation=child.estimation,
            status=child_statuses.get(child.id, "open"),
            depends_on_task_id=child.depends_on_task_id,
        ) for child in children],
    )


def publish_task(session: Session, task_id: uuid.UUID) -> None:
    row = tasks_store.get(session, task_id)
    if row is None:
        # Mutation deleted the row out from under us (orphan close). Treat it
        # as a removal so the client drops it either way.
        publish_task_removed(task_id)
        return
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    data = task_read(session, row, status_, is_manual).model_dump(mode="json")
    _emit({"type": "task", "data": data})


def publish_task_removed(task_id: uuid.UUID) -> None:
    _emit({"type": "task_removed", "id": str(task_id)})


def publish_input(session: Session, raw_input_id: uuid.UUID) -> None:
    row = raw_inputs_store.get(session, raw_input_id)
    if row is None:
        return
    _emit({"type": "input", "data": RawInputRead.from_row(row).model_dump(mode="json")})


def publish_points(session: Session) -> None:
    _emit({"type": "points", "total": points_store.total(session)})


def publish_kotx() -> None:
    _emit({"type": "kotx"})
