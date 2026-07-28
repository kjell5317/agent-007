"""Shared test scaffolding.

The runner tests drive the agent with hand-rolled fake sessions
(`SimpleNamespace`), which can't serve queries. Each agent run now reads the
label catalog from the DB — for the tool enum, the calendar mirror's
`eventLabelId` and the kotx repo mapping — so stub those lookups out by
default. Tests that exercise labels override them in the test body.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.db.clients import labels as labels_store  # noqa: E402


@pytest.fixture(autouse=True)
def _empty_label_catalog(monkeypatch):
    monkeypatch.setattr(labels_store, "agent_descriptions", lambda session: {})
    monkeypatch.setattr(labels_store, "google_id_for", lambda session, name: None)
    monkeypatch.setattr(labels_store, "get_by_repo", lambda session, repo: None)
