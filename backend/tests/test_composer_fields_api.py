"""The composer's due date / label / estimation pills at the HTTP boundary."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import tasks as tasks_api
from app.api import work as work_api
from app.db import get_session
from app.db.clients import labels as labels_store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(labels_store, "exists", lambda _session, name: name == "Admin")
    app = FastAPI()
    app.include_router(work_api.router)
    app.include_router(tasks_api.router)
    app.dependency_overrides[get_session] = lambda: object()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def created(monkeypatch):
    calls = []

    async def fake_create(_session, user_fields):
        calls.append(user_fields)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(tasks_api, "create_manual_task", fake_create)
    return calls


def test_manual_create_hands_the_pill_values_to_the_pipeline(client, created):
    response = client.post("/tasks", json={
        "content": "Renew passport",
        "due_date": "2026-11-20T18:00:00+01:00",
        "label": "Admin",
        "estimation": 45,
    })

    assert response.status_code == 202
    assert created == [{
        "content": "Renew passport",
        "due_date": datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc),
        "label": "Admin",
        "estimation": 45,
    }]


@pytest.mark.parametrize("body", [
    {"label": "Nope"},
    {"estimation": 0},
    {"estimation": -15},
    {"due_date": "next friday"},
])
def test_manual_create_rejects_bad_pill_values(client, created, body):
    response = client.post("/tasks", json={"content": "Renew passport", **body})

    assert response.status_code == 422
    assert created == []


def test_logged_work_rejects_an_unknown_label(client, monkeypatch):
    async def never(*_args, **_kwargs):
        raise AssertionError("an unknown label must not create a task")

    monkeypatch.setattr(work_api.work, "log_spontaneous", never)

    response = client.post("/tasks/logged", json={
        "title": "Renew passport",
        "started_at": "2026-11-17T09:00:00+00:00",
        "ended_at": "2026-11-17T09:30:00+00:00",
        "label": "Nope",
    })

    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown label"
