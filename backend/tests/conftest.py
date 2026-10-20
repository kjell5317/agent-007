"""Shared test scaffolding.

The runner tests drive the agent with hand-rolled fake sessions
(`SimpleNamespace`), which can't serve queries. Each agent run now reads the
label catalog from the DB — for the tool enum, the calendar mirror's
`eventLabelId`, the kotx repo mapping and the work type / effort defaults —
so stub those lookups out by default. Tests that exercise labels override
them in the test body. The related-calendar-events lookup (DB query, possibly
an embedding call) is stubbed to "no events" the same way, and the learned
duration model a new task's estimate is corrected with to "none fitted yet".
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent import retrieval  # noqa: E402
from app.db.clients import labels as labels_store  # noqa: E402
from app.services.learn import jobs as learn_jobs  # noqa: E402


@pytest.fixture(autouse=True)
def _empty_label_catalog(monkeypatch):
    monkeypatch.setattr(labels_store, "agent_descriptions", lambda session: {})
    monkeypatch.setattr(labels_store, "google_id_for", lambda session, name: None)
    monkeypatch.setattr(labels_store, "get_by_repo", lambda session, repo: None)
    monkeypatch.setattr(labels_store, "defaults_by_name", lambda session: {})


@pytest.fixture(autouse=True)
def _no_related_calendar_events(monkeypatch):
    async def none(session, *, raw, embedding=None):
        return []

    monkeypatch.setattr(retrieval, "related_calendar_events", none)


@pytest.fixture(autouse=True)
def _no_learned_durations(monkeypatch):
    monkeypatch.setattr(learn_jobs, "duration_params", lambda session: None)
