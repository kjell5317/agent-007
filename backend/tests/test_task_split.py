from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.task import split  # noqa: E402


@pytest.mark.asyncio
async def test_short_task_can_decline_split(monkeypatch):
    task = SimpleNamespace(
        title="Email the proposal", description=None, estimation=30,
        due_date=datetime(2026, 10, 2, tzinfo=timezone.utc),
        related_event_calendar_id=None, related_event_id=None,
    )
    seen = {}

    async def fake_chat(*_args, **kwargs):
        seen["prompt"] = kwargs["system_prompt"]
        return SimpleNamespace(tool_calls=[SimpleNamespace(
            name="propose_subtasks", input={"subtasks": []},
        )])

    monkeypatch.setattr(split, "chat", fake_chat)
    plans = await split.propose_subtasks(task)

    assert plans == []
    assert "only separate actions explicitly stated" in seen["prompt"]


@pytest.mark.asyncio
async def test_split_step_requires_parent_evidence(monkeypatch):
    task = SimpleNamespace(
        title="Write proposal and email it", description=None, estimation=60,
        due_date=datetime(2026, 10, 2, tzinfo=timezone.utc),
        related_event_calendar_id=None, related_event_id=None,
    )

    async def fake_chat(*_args, **_kwargs):
        return SimpleNamespace(tool_calls=[SimpleNamespace(
            name="propose_subtasks", input={"subtasks": [
                {"title": "Write proposal", "estimation": 30, "source_excerpt": "Write proposal"},
                {"title": "Research competitors", "estimation": 30, "source_excerpt": "Research competitors"},
            ]},
        )])

    monkeypatch.setattr(split, "chat", fake_chat)
    with pytest.raises(ValueError, match="source evidence"):
        await split.propose_subtasks(task)
