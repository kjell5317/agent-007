"""Public task codes stay unique and subtask codes survive reorder."""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.db.clients import tasks as tasks_store  # noqa: E402
from app.db.models.task import Task  # noqa: E402
from app.db.schemas.task import TaskCreate  # noqa: E402


def test_public_codes_are_unique_and_subtask_numbers_stay_with_the_task():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Task.__table__.create(engine)
    with Session(engine) as session:
        parents = [tasks_store.create(session, TaskCreate(title=f"Parent {n}")) for n in range(20)]
        codes = [task.public_id for task in parents]
        assert len(set(codes)) == 20
        assert all(code is not None and len(code) == 3 and code.isupper() for code in codes)
        parent = parents[0]
        assert tasks_store.get_by_public_id(session, parent.public_id.lower()).id == parent.id
        first = tasks_store.create(session, TaskCreate(title="First", parent_task_id=parent.id))
        second = tasks_store.create(session, TaskCreate(title="Second", parent_task_id=parent.id))
        assert (first.public_id, second.public_id) == (f"{parent.public_id}1", f"{parent.public_id}2")
        first.subtask_order, second.subtask_order = 1, 0
        session.flush()
        assert (first.public_id, second.public_id) == (f"{parent.public_id}1", f"{parent.public_id}2")
