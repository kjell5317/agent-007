"""The agent puts a task on hold while the user waits on someone, or resumes it."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import dispatch  # noqa: E402
from app.agent.helpers.text import task_field_lines  # noqa: E402
from app.agent.tools import new_input_tools, thread_followup_tools  # noqa: E402

SINCE = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, uuid.UUID]]:
    seen: list[tuple[str, uuid.UUID]] = []

    async def wait(_session, task):
        seen.append(("wait", task.id))

    async def resume(_session, task):
        seen.append(("resume", task.id))

    async def update(_session, task_id, patch, **_kw):
        seen.append(("update", task_id))

    async def close(_session, task_id):
        seen.append(("close", task_id))

    monkeypatch.setattr(dispatch, "wait_task", wait)
    monkeypatch.setattr(dispatch, "resume_task", resume)
    monkeypatch.setattr(dispatch, "update_task_svc", update)
    monkeypatch.setattr(dispatch, "close_task_svc", close)
    monkeypatch.setattr(
        dispatch.tasks_store, "latest_status_for", lambda _s, ids: {i: "open" for i in ids},
    )
    return seen


def _task():
    return SimpleNamespace(id=uuid.uuid4(), title="Sign the lease", due_date=None)


@pytest.mark.asyncio
async def test_waiting_goes_on_hold_before_the_edits(calls):
    task = _task()

    result = await dispatch.apply_task_action(
        SimpleNamespace(), task, "update_task",
        {"waiting": True, "title": "Sign the lease (Anna sends it)", "reason": "Anna will send it"},
    )

    assert calls == [("wait", task.id), ("update", task.id)]
    assert result["outcome"] == "updated" and result["waiting"] is True


@pytest.mark.asyncio
async def test_resume_comes_after_the_edits(calls):
    task = _task()

    result = await dispatch.apply_task_action(
        SimpleNamespace(), task, "update_task", {"waiting": False, "estimation": 30},
    )

    assert calls == [("update", task.id), ("resume", task.id)]
    assert result["waiting"] is False


@pytest.mark.asyncio
async def test_a_close_wins_over_waiting(calls):
    task = _task()

    await dispatch.apply_task_action(
        SimpleNamespace(), task, "update_task",
        {"status": "closed", "waiting": True}, user_requested=True,
    )

    assert calls == [("close", task.id)]


@pytest.mark.asyncio
async def test_a_task_that_cant_wait_is_left_alone(calls, monkeypatch):
    async def refuse(_session, _task):
        raise ValueError("A parent task is worked on through its subtasks")

    monkeypatch.setattr(dispatch, "wait_task", refuse)

    result = await dispatch.apply_task_action(SimpleNamespace(), _task(), "update_task", {"waiting": True})

    assert calls == [] and result["waiting"] is None


def test_waiting_is_not_patched_onto_the_task():
    assert "waiting" in dispatch._NON_PATCH_FIELDS


def test_every_update_task_offers_waiting():
    labels = {"Home": "Household"}
    for tools in (new_input_tools(labels), thread_followup_tools(labels)):
        update = next(tool for tool in tools if tool["name"] == "update_task")
        assert update["parameters"]["properties"]["waiting"]["type"] == "boolean"


def test_the_agent_sees_that_a_task_is_waiting():
    task = SimpleNamespace(
        id=uuid.uuid4(), title="Sign the lease", description=None, due_date=None,
        scheduled_date=None, estimation=None, location=None, link=None, label=None,
        schedule_state="waiting", waiting_since=SINCE,
    )
    assert f"  waiting since: {SINCE.isoformat()}" in task_field_lines(task)

    task.schedule_state, task.waiting_since = "placed", None
    assert not any("waiting" in line for line in task_field_lines(task))
