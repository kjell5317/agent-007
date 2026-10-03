"""Dismissing a parent persists the whole group's status before publishing."""

import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.task import dismiss  # noqa: E402


def test_dismiss_container_updates_parent_and_children_together(monkeypatch):
    parent = SimpleNamespace(id=uuid.uuid4(), is_container=True)
    children = [SimpleNamespace(id=uuid.uuid4()) for _ in range(2)]
    inputs = {task.id: SimpleNamespace(id=uuid.uuid4(), status="open") for task in [parent, *children]}
    published = []

    class FakeSession:
        def commit(self):
            assert all(item.status == "not_task" for item in inputs.values())
            published.append("commit")

    session = FakeSession()
    monkeypatch.setattr(dismiss.tasks_store, "get", lambda _session, _id: parent)
    monkeypatch.setattr(dismiss.tasks_store, "children", lambda _session, _id: children)
    monkeypatch.setattr(dismiss.raw_inputs_store, "latest_for_task", lambda _session, task_id: inputs[task_id])
    monkeypatch.setattr(dismiss, "publish_task", lambda _session, task_id: published.append(task_id))
    monkeypatch.setattr(dismiss, "publish_input", lambda _session, input_id: published.append(input_id))

    assert dismiss.dismiss_container(session, parent.id) == [parent.id, children[0].id, children[1].id]
    assert published[0] == "commit"
    assert set(published[1::2]) == {parent.id, children[0].id, children[1].id}
