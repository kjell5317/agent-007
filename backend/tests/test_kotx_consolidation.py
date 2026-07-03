from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.kotx import runner as kotx_runner  # noqa: E402
from app.api.webhooks import _verify_signature  # noqa: E402
from app.services.input.gmail.preprocess import _apply_github_identity  # noqa: E402
from app.services.input.kotx.normalize import (  # noqa: E402
    envelope_for_transition,
    github_thread_key,
    parse_github_subject,
)


def _kotx_task(**overrides) -> dict:
    base = {
        "id": 42,
        "repo": "owner/repo",
        "subjectType": "issue",
        "subjectNumber": 31,
        "title": "Add a metadata index",
        "kind": "implement",
        "state": "draft",
        "status": "awaiting approval",
        "attempt": 1,
        "proposes": None,
        "githubUrl": "https://github.com/owner/repo/issues/31",
        "prNumber": None,
        "trackedPrNumber": None,
        "branch": "31-add-metadata-index",
        "updatedAt": "2026-07-03T10:00:00.000Z",
    }
    base.update(overrides)
    return base


# --- normalize -----------------------------------------------------------------


def test_envelope_carries_github_thread_key_and_dedup_id():
    env = envelope_for_transition(_kotx_task(), doc="# TASK\ndo the thing")
    assert env is not None
    assert env.source == "kotx"
    assert env.external_id == "42:1:draft:"
    assert env.source_metadata["thread_id"] == "github:owner/repo#31"
    assert env.source_metadata["kotx_task_id"] == 42
    assert "do the thing" in env.content


def test_envelope_distinguishes_pr_and_merge_proposals():
    pr = envelope_for_transition(_kotx_task(state="awaiting_approval", proposes="pr"))
    merge = envelope_for_transition(
        _kotx_task(state="awaiting_approval", proposes="merge")
    )
    assert pr is not None and merge is not None
    assert pr.external_id != merge.external_id


def test_resolve_conflict_runs_are_skipped():
    assert envelope_for_transition(_kotx_task(kind="resolve_conflict")) is None


def test_parse_github_subject_rejects_number_prefix_match():
    assert parse_github_subject("https://github.com/o/r/issues/31") == ("o/r", 31)
    assert parse_github_subject("https://github.com/o/r/issues/310") != ("o/r", 31)
    assert parse_github_subject("https://example.com/x") is None


# --- webhook signature -----------------------------------------------------------


def test_webhook_signature_roundtrip():
    body = json.dumps({"task": _kotx_task()}).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert _verify_signature(body, sig, "secret")
    assert not _verify_signature(body, sig, "other")
    assert not _verify_signature(body, "sha256=deadbeef", "secret")
    assert not _verify_signature(body, None, "secret")


# --- gmail github identity --------------------------------------------------------


def test_github_email_gets_canonical_thread_key_and_reason():
    metadata = {
        "from": "Kjell <notifications@github.com>",
        "thread_id": "gmail-thread-1",
        "urls": ["https://github.com/owner/repo/pull/7#issuecomment-1"],
    }
    _apply_github_identity(metadata, {"x-github-reason": "review_requested"})
    assert metadata["thread_id"] == "github:owner/repo#7"
    assert metadata["gmail_thread_id"] == "gmail-thread-1"
    assert metadata["github_reason"] == "review_requested"
    assert metadata["github_repo"] == "owner/repo"


def test_non_github_email_is_untouched():
    metadata = {"from": "alice@example.com", "thread_id": "t1", "urls": []}
    _apply_github_identity(metadata, {})
    assert metadata["thread_id"] == "t1"
    assert "github_reason" not in metadata


# --- kotx runner state machine ----------------------------------------------------


def _raw(meta: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-00000000aaaa"),
        source="kotx",
        source_metadata=meta,
        content="kotx transition",
    )


def _meta(**overrides) -> dict:
    env = envelope_for_transition(_kotx_task(**overrides))
    assert env is not None
    return env.source_metadata


@pytest.mark.asyncio
async def test_done_transition_closes_linked_task_without_discard(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=42, link=None)
    closed = {}

    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )

    async def fake_close(session, task_id, *, discard_kotx=True):
        closed["task_id"] = task_id
        closed["discard_kotx"] = discard_kotx

    finalized = {}

    def fake_finalize(session, raw_id, **kw):
        finalized.update(kw)

    monkeypatch.setattr(kotx_runner, "close_task", fake_close)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", fake_finalize)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, _raw(_meta(state="done")))

    assert trace["outcome"] == "closed"
    assert closed == {"task_id": task.id, "discard_kotx": False}
    assert finalized["status"] == "duplicate"
    assert finalized["task_id"] == task.id


@pytest.mark.asyncio
async def test_review_sent_marks_task_done(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )
    called = {}

    async def fake_close(session, task_id, *, discard_kotx=True):
        called["closed"] = True

    monkeypatch.setattr(kotx_runner, "close_task", fake_close)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(kind="review", state="awaiting_external"))
    )
    assert trace["outcome"] == "closed"
    assert called.get("closed")


@pytest.mark.asyncio
async def test_informational_transition_without_task_is_not_task(monkeypatch):
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None
    )
    monkeypatch.setattr(
        kotx_runner.tasks, "github_link_candidates", lambda s, r, n: []
    )
    finalized = {}
    monkeypatch.setattr(
        kotx_runner.raw_inputs,
        "finalize",
        lambda session, raw_id, **kw: finalized.update(kw),
    )

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(state="running"))
    )
    assert trace["outcome"] == "not_task"
    assert finalized["status"] == "not_task"


@pytest.mark.asyncio
async def test_actionable_transition_adopts_task_by_github_link(monkeypatch):
    task = SimpleNamespace(
        id=uuid.uuid4(),
        kotx_task_id=None,
        link="https://github.com/owner/repo/issues/31",
    )
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None
    )
    monkeypatch.setattr(
        kotx_runner.tasks, "github_link_candidates", lambda s, r, n: [task]
    )
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, _raw(_meta(state="draft")))

    assert task.kotx_task_id == 42
    assert trace["adopted"] is True
    assert trace["matched_by"] == "github_link"
    assert trace["outcome"] == "no_change"


@pytest.mark.asyncio
async def test_actionable_transition_reopens_closed_task(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "closed"}
    )
    reopened = {}

    async def fake_reopen(session, task_id):
        reopened["task_id"] = task_id

    monkeypatch.setattr(kotx_runner, "reopen_task", fake_reopen)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(state="awaiting_approval", proposes="merge"))
    )
    assert trace["outcome"] == "reopened"
    assert reopened["task_id"] == task.id


@pytest.mark.asyncio
async def test_new_actionable_transition_creates_task_via_agent(monkeypatch):
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None
    )
    monkeypatch.setattr(
        kotx_runner.tasks, "github_link_candidates", lambda s, r, n: []
    )

    async def fake_extract(session, raw):
        return {
            "title": "#31 Add a metadata index",
            "estimation": 20,
            "due_date": "2026-07-04T12:00:00+02:00",
            "label": "SocialAI",
        }

    created = {}

    def fake_create(session, payload):
        created["payload"] = payload
        return SimpleNamespace(
            id=uuid.uuid4(), title=payload.title, kotx_task_id=None, link=payload.link
        )

    async def fake_schedule(session, task):
        created["scheduled"] = True

    finalized = {}
    monkeypatch.setattr(kotx_runner, "extract_task_fields", fake_extract)
    monkeypatch.setattr(kotx_runner, "load_labels", lambda: {"CSEE": None})
    monkeypatch.setattr(kotx_runner.tasks, "create", fake_create)
    monkeypatch.setattr(kotx_runner, "schedule_task", fake_schedule)
    monkeypatch.setattr(
        kotx_runner.raw_inputs,
        "finalize",
        lambda session, raw_id, **kw: finalized.update(kw),
    )

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, _raw(_meta(state="draft")))

    assert trace["outcome"] == "task_created"
    payload = created["payload"]
    # Deterministic fields: title from the github subject minus the repo,
    # no description / location / link — the frontend's run section carries
    # that context.
    assert payload.title == "#31 Add a metadata index"
    assert payload.description is None
    assert payload.location is None
    assert payload.link is None
    # Extracted fields plus the agent's label (no configured label matches
    # "owner/repo").
    assert payload.estimation == 20
    assert payload.label == "SocialAI"
    assert created["scheduled"] is True
    assert finalized["status"] == "open"


def test_label_for_repo_prefers_config_match(monkeypatch):
    monkeypatch.setattr(
        kotx_runner, "load_labels", lambda: {"Uni": None, "CSEE": None, "SocialAI": None}
    )
    assert kotx_runner._label_for_repo("askLio/CSEE-strategic-negotiation-agent") == "CSEE"
    # Alphanumeric-only comparison bridges hyphenated org names.
    assert kotx_runner._label_for_repo("TUM-Social-AI/AflaConnect") == "SocialAI"
    assert kotx_runner._label_for_repo("owner/repo") is None
