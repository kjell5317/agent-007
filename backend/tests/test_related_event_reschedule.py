from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.services.calendar import discover
from app.services.task import update as task_update


@pytest.mark.asyncio
async def test_moved_event_updates_only_derived_task_deadline(monkeypatch):
    start = datetime.now(timezone.utc) + timedelta(days=3)
    task = SimpleNamespace(id=uuid.uuid4(), due_date=start - timedelta(days=1))
    event = SimpleNamespace(id="xy", calendar_id="primary", start=start)
    updates = []

    class Session:
        def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [task]))

    async def fake_update(_session, task_id, fields):
        updates.append((task_id, fields))

    monkeypatch.setattr(discover, "is_managed_event", lambda _event: False)
    monkeypatch.setattr(
        discover, "get_settings", lambda: SimpleNamespace(event_buffer_minutes=15)
    )
    monkeypatch.setattr(task_update, "update_task", fake_update)
    await discover._refresh_related_task_deadlines(Session(), [event], [])

    assert updates == [(
        task.id,
        {"due_date": start - timedelta(minutes=15), "related_event_due_derived": True},
    )]
