"""A parent task finishes with its subtasks and comes back with them."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.task import close, dismiss, reopen


class _Session:
    def commit(self):
        pass


def _task(*, container: bool = False, parent=None):
    return SimpleNamespace(id=uuid.uuid4(), is_container=container, parent_task_id=parent)


def _install_statuses(monkeypatch, module, children, statuses):
    monkeypatch.setattr(module.tasks_store, "children", lambda _s, _id: children)
    monkeypatch.setattr(
        module.tasks_store, "latest_status_for",
        lambda _s, ids: {i: statuses[i] for i in ids if i in statuses},
    )


@pytest.fixture
def settled(monkeypatch):
    calls: list[tuple[str, uuid.UUID]] = []

    async def fake_close(_s, task_id, **kwargs):
        calls.append(("close", task_id))

    async def fake_dismiss(_s, task_id, **kwargs):
        calls.append(("dismiss", task_id))

    monkeypatch.setattr(close, "close_task", fake_close)
    monkeypatch.setattr(dismiss, "dismiss_task", fake_dismiss)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_statuses", "expected"),
    [
        (["closed", "not_task"], "close"),
        (["closed", "closed"], "close"),
        (["not_task", "not_task"], "dismiss"),
        (["closed", "open"], None),
    ],
)
async def test_parent_settles_once_no_subtask_is_open(monkeypatch, settled, child_statuses, expected):
    parent = _task(container=True)
    children = [_task(parent=parent.id) for _ in child_statuses]
    statuses = {child.id: status for child, status in zip(children, child_statuses)}
    _install_statuses(monkeypatch, close, children, {**statuses, parent.id: "open"})

    await close.settle_parent(_Session(), parent.id)

    assert settled == ([(expected, parent.id)] if expected else [])


@pytest.mark.asyncio
async def test_finished_parent_is_left_alone(monkeypatch, settled):
    parent = _task(container=True)
    child = _task(parent=parent.id)
    _install_statuses(monkeypatch, close, [child], {child.id: "closed", parent.id: "closed"})

    await close.settle_parent(_Session(), parent.id)

    assert settled == []


@pytest.fixture
def reopen_env(monkeypatch):
    anchors: dict[uuid.UUID, SimpleNamespace] = {}
    placed: list[list[uuid.UUID]] = []
    scheduled: list[uuid.UUID] = []
    tasks: dict[uuid.UUID, SimpleNamespace] = {}

    async def no_drop(_s, _ids):
        pass

    async def fake_schedule(_s, task):
        scheduled.append(task.id)

    async def fake_place(_s, _parent, children):
        placed.append([child.id for child in children])

    monkeypatch.setattr(reopen.tasks_store, "get", lambda _s, task_id: tasks.get(task_id))
    monkeypatch.setattr(reopen.raw_inputs_store, "latest_for_task", lambda _s, task_id: anchors.get(task_id))
    monkeypatch.setattr(reopen, "drop_instances", no_drop)
    monkeypatch.setattr(reopen, "surplus_after_reopen", lambda *_args: [])
    monkeypatch.setattr(reopen, "schedule_task", fake_schedule)
    monkeypatch.setattr(reopen, "place_subtasks", fake_place)
    monkeypatch.setattr(reopen, "publish_input", lambda *_args: None)
    monkeypatch.setattr(reopen, "publish_task", lambda *_args: None)

    def add(task, status):
        tasks[task.id] = task
        anchors[task.id] = SimpleNamespace(id=uuid.uuid4(), status=status, processed_at=None)

    def statuses(_s, ids):
        return {i: anchors[i].status for i in ids if i in anchors}

    monkeypatch.setattr(reopen.tasks_store, "latest_status_for", statuses)
    return SimpleNamespace(add=add, anchors=anchors, placed=placed, scheduled=scheduled)


@pytest.mark.asyncio
async def test_reopening_a_completed_parent_revives_its_completed_subtasks(monkeypatch, reopen_env):
    parent = _task(container=True)
    done, dropped = _task(parent=parent.id), _task(parent=parent.id)
    reopen_env.add(parent, "closed")
    reopen_env.add(done, "closed")
    reopen_env.add(dropped, "not_task")
    monkeypatch.setattr(reopen.tasks_store, "children", lambda _s, _id: [done, dropped])

    await reopen.reopen_task(_Session(), parent.id)

    assert reopen_env.anchors[parent.id].status == "open"
    assert reopen_env.anchors[done.id].status == "open"
    assert reopen_env.anchors[dropped.id].status == "not_task"
    assert reopen_env.placed == [[done.id]]
    assert reopen_env.scheduled == []


@pytest.mark.asyncio
async def test_reopening_a_subtask_revives_its_parent_but_not_its_siblings(monkeypatch, reopen_env):
    parent = _task(container=True)
    child, sibling = _task(parent=parent.id), _task(parent=parent.id)
    reopen_env.add(parent, "closed")
    reopen_env.add(child, "closed")
    reopen_env.add(sibling, "closed")
    monkeypatch.setattr(reopen.tasks_store, "children", lambda _s, _id: [child, sibling])

    await reopen.reopen_task(_Session(), child.id)

    assert reopen_env.anchors[parent.id].status == "open"
    assert reopen_env.anchors[child.id].status == "open"
    assert reopen_env.anchors[sibling.id].status == "closed"
    assert reopen_env.scheduled == [child.id]
    assert reopen_env.placed == []
