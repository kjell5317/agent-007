from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import pytest

from app.agent.chat import runner
from app.db.models.raw_input import RawInput


@pytest.mark.asyncio
async def test_chat_created_task_has_embedded_linked_input(monkeypatch):
    rows = []
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, title="Prepare for XY", description="Draft slides")

    class Session:
        def add(self, row):
            rows.append(row)

        def commit(self):
            pass

    monkeypatch.setattr(runner.tasks_store, "create", lambda *_args: task)

    async def fake_embed(_text):
        return [0.1]

    async def fake_schedule(_session, _task):
        pass

    monkeypatch.setattr(runner, "embed", fake_embed)
    monkeypatch.setattr(runner, "schedule_task", fake_schedule)
    due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    result, _trace = await runner._create_task(
        Session(), {"title": task.title, "description": task.description,
                    "estimation": 30, "due_date": due}
    )
    assert "Created task" in result
    assert len(rows) == 1
    assert isinstance(rows[0], RawInput)
    assert rows[0].task_id == task_id
    assert rows[0].embedding == [0.1]
    assert rows[0].source == "chat"
