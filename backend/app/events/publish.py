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
from app.db.schemas.task import TaskRead
from app.events import bus


def _emit(event: dict) -> None:
    bus.publish(json.dumps(event))


def publish_task(session: Session, task_id: uuid.UUID) -> None:
    row = tasks_store.get(session, task_id)
    if row is None:
        # Mutation deleted the row out from under us (orphan close). Treat it
        # as a removal so the client drops it either way.
        publish_task_removed(task_id)
        return
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    _emit({"type": "task", "data": TaskRead.build(row, status_, is_manual).model_dump(mode="json")})


def publish_task_removed(task_id: uuid.UUID) -> None:
    _emit({"type": "task_removed", "id": str(task_id)})


def publish_input(session: Session, raw_input_id: uuid.UUID) -> None:
    row = raw_inputs_store.get(session, raw_input_id)
    if row is None:
        return
    _emit({"type": "input", "data": RawInputRead.from_row(row).model_dump(mode="json")})


def publish_points(session: Session) -> None:
    _emit({"type": "points", "total": points_store.total(session)})
