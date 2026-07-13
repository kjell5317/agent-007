from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.kotx import runner as kotx_runner  # noqa: E402
from app.api import notifications as notif  # noqa: E402
from app.api.webhooks import _verify_signature  # noqa: E402
from app.services import notify as notify_svc  # noqa: E402
from app.services.kotx import client as kotx_client  # noqa: E402
from app.services.kotx import discard as kotx_discard  # noqa: E402
from app.services.input.kotx import poll as kotx_poll  # noqa: E402
from app.services.input.gmail.preprocess import _apply_github_identity  # noqa: E402
from app.services.input.kotx.normalize import (  # noqa: E402
    display_assignee_for,
    envelope_for_transition,
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
    env = envelope_for_transition(
        _kotx_task(
            stateReason="waiting for user approval",
            triggerReason="assigned to issue",
            assigned=["", "octocat"],
        ),
        doc="# TASK\ndo the thing",
    )
    assert env is not None
    assert env.source == "kotx"
    assert env.external_id == "42:1:draft:"
    assert env.source_metadata["thread_id"] == "github:owner/repo#31"
    assert env.source_metadata["kotx_task_id"] == 42
    assert env.source_metadata["state_reason"] == "waiting for user approval"
    assert env.source_metadata["trigger_reason"] == "assigned to issue"
    assert env.source_metadata["assignee"] == "octocat"
    assert env.content == "# TASK\ndo the thing"
    assert "kotx implement" not in env.content
    assert "State:" not in env.content
    assert "Reason:" not in env.content


def test_envelope_truncates_document_content_without_header():
    env = envelope_for_transition(_kotx_task(), doc="x" * 6001)

    assert env is not None
    assert env.content == "x" * 6000


def test_envelope_distinguishes_pr_and_merge_proposals():
    pr = envelope_for_transition(_kotx_task(state="awaiting_approval", proposes="pr"))
    merge = envelope_for_transition(
        _kotx_task(state="awaiting_approval", proposes="merge")
    )
    assert pr is not None and merge is not None
    assert pr.external_id != merge.external_id


def test_display_assignee_uses_assigned_then_compat_assignees():
    assert display_assignee_for(_kotx_task(assigned=["  monalisa  "])) == "monalisa"
    assert display_assignee_for(_kotx_task(assigned=[], assignees=["octocat"])) == "octocat"
    assert display_assignee_for(_kotx_task(assigned=[])) == "unassigned"


def test_resolve_conflict_runs_are_ingested_with_thread_metadata_and_dedup_id():
    env = envelope_for_transition(
        _kotx_task(
            kind="resolve_conflict",
            state="running",
            status="resolving conflicts",
            attempt=2,
        )
    )

    assert env is not None
    assert env.source == "kotx"
    assert env.external_id == "42:2:running:"
    assert env.source_metadata["thread_id"] == "github:owner/repo#31"
    assert env.source_metadata["kotx_task_id"] == 42
    assert env.source_metadata["kotx_kind"] == "resolve_conflict"
    assert env.source_metadata["github_url"] == "https://github.com/owner/repo/issues/31"
    assert env.content == ""


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


# --- kotx poll -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kotx_poll_fetches_all_scope(monkeypatch):
    latest = kotx_poll.datetime(2026, 7, 3, 10, 0, tzinfo=kotx_poll.timezone.utc)
    calls = {}

    class FakeResult:
        def scalar_one(self):
            return latest

    class FakeSession:
        def execute(self, stmt):
            calls["stmt"] = stmt
            return FakeResult()

    async def fake_fetch_tasks(*, updated_since=None, scope="active"):
        calls["updated_since"] = updated_since
        calls["scope"] = scope
        return []

    monkeypatch.setattr(kotx_poll.kotx_client, "fetch_tasks", fake_fetch_tasks)

    summary = await kotx_poll.poll(FakeSession(), account_key=None)

    assert summary["fetched"] == 0
    assert calls["scope"] == "all"
    assert calls["updated_since"] == latest - kotx_poll._OVERLAP


# --- kotx runner state machine ----------------------------------------------------


def _raw(meta: dict, raw_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=raw_id or uuid.UUID("00000000-0000-0000-0000-00000000aaaa"),
        source="kotx",
        source_metadata=meta,
        content="kotx transition",
    )


def _meta(**overrides) -> dict:
    env = envelope_for_transition(_kotx_task(**overrides))
    assert env is not None
    return env.source_metadata


@pytest.fixture(autouse=True)
def _no_thread_backfill(monkeypatch):
    monkeypatch.setattr(
        kotx_runner.raw_inputs,
        "link_unassigned_by_thread",
        lambda *_args, **_kwargs: 0,
    )


@pytest.fixture(autouse=True)
def _unprocessed_by_default(monkeypatch):
    """The post-lock redelivery guard does a fresh DB read; the state-machine
    tests run on SimpleNamespace sessions with no `execute`, so default it to
    "not yet processed" and let the redelivery test override it."""
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "processing_state", lambda *_a, **_k: None
    )


@pytest.fixture(autouse=True)
def kotx_prompts(monkeypatch):
    """Capture the HA prompts the runner sends in place of kotx's removed ones,
    and keep every test hermetic (no real notify() regardless of CWD/.env)."""
    calls: list[dict] = []

    def _recorder(name: str):
        async def _fn(task, **extra):
            calls.append({"prompt": name, "task_id": task.id, **extra})

        return _fn

    for name in ("start", "open_pr", "confirm_merge", "review_ready"):
        monkeypatch.setattr(kotx_runner, f"notify_kotx_{name}", _recorder(name))

    async def _noop_clear(task_id):
        pass

    monkeypatch.setattr(kotx_runner, "clear_task_notification", _noop_clear)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "award_points"), [("done", True), ("cancelled", False)])
async def test_terminal_transition_closes_linked_task_without_discard(
    monkeypatch, state, award_points
):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=42, link=None)
    closed = {}

    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )

    async def fake_close(session, task_id, *, discard_kotx=True, award_points=True):
        closed["task_id"] = task_id
        closed["discard_kotx"] = discard_kotx
        closed["award_points"] = award_points

    finalized = {}

    def fake_finalize(session, raw_id, **kw):
        finalized.update(kw)

    monkeypatch.setattr(kotx_runner, "close_task", fake_close)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", fake_finalize)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, _raw(_meta(state=state)))

    assert trace["outcome"] == "closed"
    assert closed == {
        "task_id": task.id,
        "discard_kotx": False,
        "award_points": award_points,
    }
    assert finalized["status"] == "duplicate"
    assert finalized["task_id"] == task.id


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["done", "cancelled"])
async def test_resolve_conflict_terminal_transition_never_closes_or_awards(
    monkeypatch, state
):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=42, link=None)

    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )

    async def fail_close(*_args, **_kwargs):
        raise AssertionError("a resolve run finishing must not close the task")

    finalized = {}

    def fake_finalize(session, raw_id, **kw):
        finalized.update(kw)

    monkeypatch.setattr(kotx_runner, "close_task", fail_close)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", fake_finalize)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(kind="resolve_conflict", state=state, status=state))
    )

    assert trace["outcome"] == "no_change"
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

    async def fake_close(session, task_id, *, discard_kotx=True, award_points=True):
        called["task_id"] = task_id
        called["discard_kotx"] = discard_kotx
        called["award_points"] = award_points

    monkeypatch.setattr(kotx_runner, "close_task", fake_close)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(kind="review", state="awaiting_external"))
    )
    assert trace["outcome"] == "closed"
    assert called == {"task_id": task.id, "discard_kotx": False, "award_points": True}


@pytest.mark.asyncio
async def test_pr_follow_up_run_reopens_task_instead_of_creating_a_second(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=42, link=None)
    prior = SimpleNamespace(task_id=task.id)

    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None
    )
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "find_kotx_by_pr", lambda s, repo, n: prior
    )
    monkeypatch.setattr(kotx_runner.tasks, "get", lambda s, tid: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "closed"}
    )

    reopened = {}

    async def fake_reopen(session, task_id):
        reopened["task_id"] = task_id

    finalized = {}
    monkeypatch.setattr(kotx_runner, "reopen_task", fake_reopen)
    monkeypatch.setattr(
        kotx_runner.raw_inputs,
        "finalize",
        lambda session, raw_id, **kw: finalized.update(kw),
    )

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session,
        _raw(_meta(id=77, subjectType="pull_request", subjectNumber=7, state="draft")),
    )

    assert trace["matched_by"] == "github_pr"
    assert trace["outcome"] == "reopened"
    assert reopened == {"task_id": task.id}
    assert finalized["status"] == "duplicate"
    assert finalized["task_id"] == task.id
    assert task.kotx_task_id == 77


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
async def test_unmatched_resolve_conflict_transition_is_visible_as_not_task(monkeypatch):
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
        session, _raw(_meta(kind="resolve_conflict", state="running"))
    )

    assert trace["outcome"] == "not_task"
    assert trace["reason"] == "kotx resolve_conflict is running; nothing to do yet"
    assert finalized["status"] == "not_task"


@pytest.mark.asyncio
async def test_trigger_reason_becomes_trace_reason(monkeypatch):
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None
    )
    monkeypatch.setattr(
        kotx_runner.tasks, "github_link_candidates", lambda s, r, n: []
    )
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "finalize", lambda session, raw_id, **kw: None
    )

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(state="running", triggerReason="new comment on PR"))
    )

    # The kotx-shipped trigger reason wins over the informational boilerplate,
    # so the inbox row carries a meaningful reason like every other source.
    assert trace["outcome"] == "not_task"
    assert trace["reason"] == "new comment on PR"


@pytest.mark.asyncio
async def test_actionable_transition_backfills_preparing_thread_inputs(monkeypatch):
    task_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        title="#31 Add a metadata index",
        kotx_task_id=None,
        link=None,
    )
    preparing_id = uuid.UUID("00000000-0000-0000-0000-00000000bbbb")
    actionable_id = uuid.UUID("00000000-0000-0000-0000-00000000cccc")
    preparing_meta = _meta(state="drafting", status="preparing task")
    actionable_meta = _meta(state="draft")
    rows = {
        preparing_id: {
            "source": "kotx",
            "thread_id": preparing_meta["thread_id"],
            "status": None,
            "task_id": None,
        },
        actionable_id: {
            "source": "kotx",
            "thread_id": actionable_meta["thread_id"],
            "status": None,
            "task_id": None,
        },
    }

    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(
        kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None
    )
    monkeypatch.setattr(
        kotx_runner.tasks, "github_link_candidates", lambda s, r, n: []
    )

    async def fake_extract(session, raw):
        return {"estimation": 20, "due_date": None, "label": None}

    def fake_create(session, payload):
        return task

    async def fake_schedule(session, created_task, **kwargs):
        assert created_task is task

    def fake_finalize(session, raw_id, **kw):
        rows[raw_id]["status"] = kw["status"]
        if "task_id" in kw:
            rows[raw_id]["task_id"] = kw["task_id"]

    def fake_link_unassigned_by_thread(session, *, source, thread_id, task_id):
        linked = 0
        for row in rows.values():
            if (
                row["source"] == source
                and row["thread_id"] == thread_id
                and row["task_id"] is None
            ):
                row["task_id"] = task_id
                linked += 1
        return linked

    monkeypatch.setattr(kotx_runner, "extract_task_fields", fake_extract)
    monkeypatch.setattr(kotx_runner.tasks, "create", fake_create)
    monkeypatch.setattr(kotx_runner, "schedule_task", fake_schedule)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", fake_finalize)
    monkeypatch.setattr(
        kotx_runner.raw_inputs,
        "link_unassigned_by_thread",
        fake_link_unassigned_by_thread,
    )

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    preparing_trace = await kotx_runner.run_kotx_transition(
        session, _raw(preparing_meta, preparing_id)
    )
    actionable_trace = await kotx_runner.run_kotx_transition(
        session, _raw(actionable_meta, actionable_id)
    )

    assert preparing_trace["outcome"] == "not_task"
    assert rows[preparing_id]["status"] == "not_task"
    assert rows[actionable_id]["status"] == "open"
    assert rows[preparing_id]["task_id"] == task_id
    assert rows[actionable_id]["task_id"] == task_id
    assert actionable_trace["backfilled_inputs"] == 1


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

    async def fake_schedule(session, task, *, primary_action=None, **kwargs):
        created["scheduled"] = True
        created["primary_action"] = primary_action

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
    # The first notification for a draft implement task swaps "Done" for "Start".
    assert created["primary_action"] == {"action": "KOTX_START", "title": "Start"}
    assert finalized["status"] == "open"


@pytest.mark.asyncio
async def test_concurrent_transitions_for_same_kotx_id_create_one_task(monkeypatch):
    # Two transitions for the same kotx task arrive at once (overlapping webhook
    # deliveries, or a webhook racing the poll). Before the per-id lock, both
    # passed the get_by_kotx_id check while the first awaited its LLM call, and
    # each created a task — tripping uq_tasks_kotx_task_id. The lock must
    # serialize them so exactly one task is created.
    import asyncio

    store: dict[int, SimpleNamespace] = {}  # visible only after commit
    creates: list[SimpleNamespace] = []

    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: store.get(i))
    monkeypatch.setattr(kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None)
    monkeypatch.setattr(kotx_runner.tasks, "github_link_candidates", lambda s, r, n: [])
    monkeypatch.setattr(kotx_runner.tasks, "latest_status_for", lambda s, ids: {})
    monkeypatch.setattr(kotx_runner, "load_labels", lambda: {})
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    async def fake_extract(session, raw):
        await asyncio.sleep(0)  # the yield point that opened the race
        return {"title": "t", "estimation": 20, "due_date": None, "label": None}

    def fake_create(session, payload):
        task = SimpleNamespace(id=uuid.uuid4(), title=payload.title, kotx_task_id=None)
        creates.append(task)
        return task

    async def fake_schedule(session, task, **kwargs):
        await asyncio.sleep(0)

    monkeypatch.setattr(kotx_runner, "extract_task_fields", fake_extract)
    monkeypatch.setattr(kotx_runner.tasks, "create", fake_create)
    monkeypatch.setattr(kotx_runner, "schedule_task", fake_schedule)

    def _session() -> SimpleNamespace:
        def commit():
            for task in creates:
                if task.kotx_task_id is not None:
                    store[task.kotx_task_id] = task

        return SimpleNamespace(commit=commit, flush=lambda: None)

    traces = await asyncio.gather(
        kotx_runner.run_kotx_transition(
            _session(), _raw(_meta(state="draft"), raw_id=uuid.uuid4())
        ),
        kotx_runner.run_kotx_transition(
            _session(), _raw(_meta(state="draft"), raw_id=uuid.uuid4())
        ),
    )

    assert len(creates) == 1
    outcomes = sorted(t["outcome"] for t in traces)
    assert outcomes == ["no_change", "task_created"]


@pytest.mark.asyncio
async def test_redelivery_finalized_by_prior_delivery_is_skipped(monkeypatch):
    # kotx retried a transition after a delivery timeout; the delivery that
    # held the lock before us already finalized the raw. The post-lock re-read
    # must short-circuit so the state machine doesn't run again and downgrade
    # the task's own open anchor to duplicate (which drops it from the list).
    def boom(*_a, **_k):
        raise AssertionError("a redelivery must not re-run the state machine")

    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", boom)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", boom)
    monkeypatch.setattr(
        kotx_runner.raw_inputs,
        "processing_state",
        lambda s, rid: (datetime.now(timezone.utc), "open"),
    )

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, _raw(_meta(state="draft")))

    assert trace["outcome"] == "already_processed"
    assert trace["status"] == "open"


@pytest.mark.asyncio
async def test_open_anchor_row_is_never_downgraded_to_duplicate(monkeypatch):
    # Reprocessing a row that is already the task's open anchor must not flip
    # it to duplicate — the derived task status reads the latest non-duplicate
    # row, so the downgrade would erase the open state and drop the task.
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task_id: "open"}
    )
    finalized: dict = {}
    monkeypatch.setattr(
        kotx_runner.raw_inputs,
        "finalize",
        lambda session, raw_id, **kw: finalized.update({"called": True, **kw}),
    )

    raw = SimpleNamespace(
        id=uuid.uuid4(),
        source="kotx",
        source_metadata=_meta(state="awaiting_approval"),
        content="kotx transition",
        status="open",
        task_id=task_id,
    )
    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, raw)

    assert trace["outcome"] == "no_change"
    assert trace["anchor_preserved"] is True
    assert "called" not in finalized


# --- HA prompts replacing kotx's removed notifications --------------------------


@pytest.mark.asyncio
async def test_open_pr_transition_sends_open_pr_prompt(monkeypatch, kotx_prompts):
    task = SimpleNamespace(
        id=uuid.uuid4(), title="#31 Add a metadata index", kotx_task_id=42, link=None
    )
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(state="awaiting_approval", proposes="pr"))
    )

    assert trace["outcome"] == "no_change"
    assert [c["prompt"] for c in kotx_prompts] == ["open_pr"]
    assert kotx_prompts[0]["task_id"] == task.id


@pytest.mark.asyncio
async def test_merge_proposal_transition_sends_confirm_merge_prompt(
    monkeypatch, kotx_prompts
):
    task = SimpleNamespace(id=uuid.uuid4(), title="t", kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    async def fake_merge_context(kotx_task_id):
        assert kotx_task_id == 42
        return {"approvedBy": "octocat", "commentMarkdown": "LGTM, ship it"}

    monkeypatch.setattr(kotx_runner.kotx_client, "fetch_merge_context", fake_merge_context)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(state="awaiting_approval", proposes="merge"))
    )

    assert trace["outcome"] == "no_change"
    assert [c["prompt"] for c in kotx_prompts] == ["confirm_merge"]
    # The reviewer + approval comment flow into the confirm-merge prompt.
    assert kotx_prompts[0]["approved_by"] == "octocat"
    assert kotx_prompts[0]["comment"] == "LGTM, ship it"


@pytest.mark.asyncio
async def test_review_awaiting_approval_sends_review_ready_prompt(
    monkeypatch, kotx_prompts
):
    task = SimpleNamespace(id=uuid.uuid4(), title="t", kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session,
        _raw(
            _meta(
                kind="review",
                state="awaiting_approval",
                assigned=["octocat"],
            )
        ),
    )

    assert trace["outcome"] == "no_change"
    assert [c["prompt"] for c in kotx_prompts] == ["review_ready"]
    assert kotx_prompts[0]["assignee"] == "octocat"


@pytest.mark.asyncio
async def test_non_prompt_transition_clears_stale_prompt(monkeypatch, kotx_prompts):
    # The run moved past its prompt state (e.g. PR opened on GitHub, work
    # resumed) — the proposed action is gone, so any lingering prompt clears.
    task = SimpleNamespace(id=uuid.uuid4(), title="t", kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)
    cleared = []

    async def fake_clear(task_id):
        cleared.append(task_id)

    monkeypatch.setattr(kotx_runner, "clear_task_notification", fake_clear)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, _raw(_meta(state="running")))

    assert trace["outcome"] == "no_change"
    assert kotx_prompts == []
    assert cleared == [task.id]


@pytest.mark.asyncio
async def test_resolve_conflict_transition_leaves_prompt_untouched(monkeypatch, kotx_prompts):
    # Auxiliary runs on the same task must not clear the primary run's prompt.
    task = SimpleNamespace(id=uuid.uuid4(), title="t", kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "open"}
    )
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)
    cleared = []

    async def fake_clear(task_id):
        cleared.append(task_id)

    monkeypatch.setattr(kotx_runner, "clear_task_notification", fake_clear)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(kind="resolve_conflict", state="running", status="resolving"))
    )

    assert trace["outcome"] == "no_change"
    assert cleared == []


@pytest.mark.asyncio
async def test_task_creation_does_not_add_prompt_over_scheduled(
    monkeypatch, kotx_prompts
):
    # Creating the 007 task schedules it, which fires the "Scheduled"
    # notification — a start/review prompt on top would be a duplicate.
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None)
    monkeypatch.setattr(kotx_runner.tasks, "github_link_candidates", lambda s, r, n: [])

    async def fake_extract(session, raw):
        return {"estimation": 20, "due_date": None, "label": None}

    scheduled = {}

    async def fake_schedule(session, task, *, primary_action=None, **kwargs):
        scheduled["primary_action"] = primary_action
        return None

    monkeypatch.setattr(kotx_runner, "extract_task_fields", fake_extract)
    monkeypatch.setattr(kotx_runner, "load_labels", lambda: {})
    monkeypatch.setattr(
        kotx_runner.tasks,
        "create",
        lambda s, payload: SimpleNamespace(
            id=uuid.uuid4(), title=payload.title, kotx_task_id=None, link=payload.link
        ),
    )
    monkeypatch.setattr(kotx_runner, "schedule_task", fake_schedule)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(session, _raw(_meta(state="draft")))

    assert trace["outcome"] == "task_created"
    # No standalone prompt — the action rides on the scheduled notification.
    assert kotx_prompts == []
    assert scheduled["primary_action"] == {"action": "KOTX_START", "title": "Start"}


@pytest.mark.asyncio
async def test_review_task_creation_drops_done_from_scheduled_notification(
    monkeypatch, kotx_prompts
):
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: None)
    monkeypatch.setattr(kotx_runner.raw_inputs, "find_by_thread", lambda s, src, t: None)
    monkeypatch.setattr(kotx_runner.tasks, "github_link_candidates", lambda s, r, n: [])

    async def fake_extract(session, raw):
        return {"estimation": 20, "due_date": None, "label": None}

    scheduled = {}

    async def fake_schedule(session, task, *, primary_action=None, **kwargs):
        scheduled["primary_action"] = primary_action
        return None

    monkeypatch.setattr(kotx_runner, "extract_task_fields", fake_extract)
    monkeypatch.setattr(kotx_runner, "load_labels", lambda: {})
    monkeypatch.setattr(
        kotx_runner.tasks,
        "create",
        lambda s, payload: SimpleNamespace(
            id=uuid.uuid4(), title=payload.title, kotx_task_id=None, link=payload.link
        ),
    )
    monkeypatch.setattr(kotx_runner, "schedule_task", fake_schedule)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(kind="review", state="awaiting_approval"))
    )

    assert trace["outcome"] == "task_created"
    assert kotx_prompts == []
    # Review has no meaningful "Done", so its scheduled notification drops the
    # leading button rather than swapping it (implement) or keeping "Done".
    assert scheduled["primary_action"] is kotx_runner.DROP_LEADING_ACTION


@pytest.mark.asyncio
async def test_reopen_does_not_add_prompt_over_scheduled(monkeypatch, kotx_prompts):
    task = SimpleNamespace(id=uuid.uuid4(), title="t", kotx_task_id=42, link=None)
    monkeypatch.setattr(kotx_runner.tasks, "get_by_kotx_id", lambda s, i: task)
    monkeypatch.setattr(
        kotx_runner.tasks, "latest_status_for", lambda s, ids: {task.id: "closed"}
    )

    async def fake_reopen(session, task_id):
        return None

    monkeypatch.setattr(kotx_runner, "reopen_task", fake_reopen)
    monkeypatch.setattr(kotx_runner.raw_inputs, "finalize", lambda *a, **k: None)

    session = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    trace = await kotx_runner.run_kotx_transition(
        session, _raw(_meta(state="awaiting_approval", proposes="pr"))
    )

    assert trace["outcome"] == "reopened"
    assert kotx_prompts == []


@pytest.mark.asyncio
async def test_kotx_action_helpers_delegate_to_post(monkeypatch):
    calls = []

    async def fake_post(kotx_task_id, verb):
        calls.append((kotx_task_id, verb))
        return True

    monkeypatch.setattr(kotx_client, "_post_action", fake_post)

    assert await kotx_client.start_task(7) is True
    assert await kotx_client.approve_task(7) is True
    assert await kotx_client.merge_task(7) is True
    assert await kotx_client.discard_task(7) is True
    assert calls == [(7, "start"), (7, "approve"), (7, "merge"), (7, "discard")]


@pytest.mark.asyncio
async def test_kotx_post_action_returns_false_when_unconfigured(monkeypatch):
    monkeypatch.setattr(kotx_client, "_base", lambda: None)
    assert await kotx_client._post_action(7, "approve") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,fn_name",
    [
        (notif.ACTION_KOTX_START, "start_task"),
        (notif.ACTION_KOTX_APPROVE, "approve_task"),
        (notif.ACTION_KOTX_MERGE, "merge_task"),
        (notif.ACTION_KOTX_COMMENT, "comment_task"),
    ],
)
async def test_notify_action_dispatches_to_kotx_and_clears(monkeypatch, action, fn_name):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=99)
    monkeypatch.setattr(notif.tasks_store, "get", lambda s, tid: task)
    called = {}

    async def fake_fn(kotx_task_id):
        called["id"] = kotx_task_id
        return True

    cleared = []

    async def fake_clear(task_id):
        cleared.append(task_id)

    monkeypatch.setattr(notif.kotx_client, fn_name, fake_fn)
    monkeypatch.setattr(notif, "clear_task_notification", fake_clear)

    payload = notif.ActionPayload(action=action, tag=f"task-{task.id}")
    result = await notif.handle_action(payload, request=SimpleNamespace(), session=object())

    assert result == {"ok": True, "action": action, "task_id": str(task.id)}
    assert called["id"] == 99
    # Kicking off the proposed action clears the prompt that offered it.
    assert cleared == [task.id]


@pytest.mark.asyncio
async def test_notify_action_merge_requires_linked_kotx_run(monkeypatch):
    task = SimpleNamespace(id=uuid.uuid4(), kotx_task_id=None)
    monkeypatch.setattr(notif.tasks_store, "get", lambda s, tid: task)

    payload = notif.ActionPayload(action=notif.ACTION_KOTX_MERGE, tag=f"task-{task.id}")
    with pytest.raises(notif.HTTPException) as exc:
        await notif.handle_action(payload, request=SimpleNamespace(), session=object())

    assert exc.value.status_code == 409


# --- notification button/message composition ------------------------------------


def _capture_notify(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_notify(title, message, **kw):
        captured.update({"title": title, "message": message, **kw})

    monkeypatch.setattr(notify_svc, "notify", fake_notify)
    return captured


@pytest.mark.asyncio
async def test_scheduled_notification_swaps_only_done_for_the_action(monkeypatch):
    captured = _capture_notify(monkeypatch)
    task = SimpleNamespace(id=uuid.uuid4(), title="#31 Add index", due_date=None)
    start = datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 4, 14, 30, tzinfo=timezone.utc)

    await notify_svc.notify_task_created(
        task, start=start, end=end, primary_action={"action": "KOTX_START", "title": "Start"}
    )

    actions = captured["actions"]
    assert actions[0] == {"action": "KOTX_START", "title": "Start"}
    # Dismiss + Reschedule are preserved; only Done was replaced.
    assert [a["action"] for a in actions[1:]] == [
        notify_svc.ACTION_DISMISS_TASK,
        notify_svc.ACTION_RESCHEDULE_TASK,
    ]
    assert "Scheduled" in captured["message"]


@pytest.mark.asyncio
async def test_scheduled_notification_keeps_done_without_action(monkeypatch):
    captured = _capture_notify(monkeypatch)
    task = SimpleNamespace(id=uuid.uuid4(), title="t", due_date=None)
    start = datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 4, 14, 30, tzinfo=timezone.utc)

    await notify_svc.notify_task_created(task, start=start, end=end)

    assert captured["actions"][0]["action"] == notify_svc.ACTION_CLOSE_TASK


@pytest.mark.asyncio
async def test_scheduled_notification_drops_done_for_review(monkeypatch):
    captured = _capture_notify(monkeypatch)
    task = SimpleNamespace(id=uuid.uuid4(), title="#31 Review", due_date=None)
    start = datetime(2026, 7, 4, 14, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 4, 14, 30, tzinfo=timezone.utc)

    await notify_svc.notify_task_created(
        task, start=start, end=end, primary_action=notify_svc.DROP_LEADING_ACTION
    )

    # "Done" is gone (it aliased "Dismiss" for a review); Dismiss + Reschedule stay.
    assert [a["action"] for a in captured["actions"]] == [
        notify_svc.ACTION_DISMISS_TASK,
        notify_svc.ACTION_RESCHEDULE_TASK,
    ]


@pytest.mark.asyncio
async def test_open_pr_notification_title_message_and_buttons(monkeypatch):
    captured = _capture_notify(monkeypatch)
    await notify_svc.notify_kotx_open_pr(SimpleNamespace(id=uuid.uuid4(), title="#31 Add index"))
    assert captured["title"] == "#31 Add index"
    assert captured["message"] == "Open Pull Request"
    assert [a["action"] for a in captured["actions"]] == [
        notify_svc.ACTION_KOTX_APPROVE,
        notify_svc.ACTION_DISMISS_TASK,
    ]


@pytest.mark.asyncio
async def test_review_ready_notification_names_assignee_without_comment_button(
    monkeypatch,
):
    captured = _capture_notify(monkeypatch)
    await notify_svc.notify_kotx_review_ready(
        SimpleNamespace(id=uuid.uuid4(), title="#31 Review index"),
        assignee="octocat",
    )

    assert captured["title"] == "#31 Review index"
    assert captured["message"] == "Comment PR of octocat"
    assert captured["actions"] == [
        {"action": notify_svc.ACTION_DISMISS_TASK, "title": "Dismiss"}
    ]


@pytest.mark.asyncio
async def test_confirm_merge_notification_names_reviewer_and_clips_comment(monkeypatch):
    captured = _capture_notify(monkeypatch)
    await notify_svc.notify_kotx_confirm_merge(
        SimpleNamespace(id=uuid.uuid4(), title="#31 Add index"),
        approved_by="octocat",
        comment="x" * 500,
    )
    assert captured["title"] == "#31 Add index"
    # Merge is the only offered action.
    assert captured["actions"] == [{"action": notify_svc.ACTION_KOTX_MERGE, "title": "Merge"}]
    lines = captured["message"].splitlines()
    assert lines[0] == "Merge Pull Request"
    assert "Approved by octocat" in captured["message"]
    assert captured["message"].endswith("…")  # comment truncated


@pytest.mark.asyncio
async def test_confirm_merge_notification_falls_back_without_context(monkeypatch):
    captured = _capture_notify(monkeypatch)
    await notify_svc.notify_kotx_confirm_merge(SimpleNamespace(id=uuid.uuid4(), title="t"))
    assert captured["message"] == "Merge Pull Request"
    assert captured["actions"] == [{"action": notify_svc.ACTION_KOTX_MERGE, "title": "Merge"}]


# --- dismiss (discard) a kotx run from the inbox --------------------------------


@pytest.mark.asyncio
async def test_discard_run_for_input_discards_by_kotx_id(monkeypatch):
    raw = _raw(_meta(state="drafting"))
    calls = {}

    async def fake_discard(kotx_task_id):
        calls["kotx_task_id"] = kotx_task_id
        return True

    monkeypatch.setattr(kotx_discard.raw_inputs_store, "get", lambda s, i: raw)
    monkeypatch.setattr(kotx_discard.kotx_client, "discard_task", fake_discard)
    monkeypatch.setattr(kotx_discard, "publish_kotx", lambda: calls.setdefault("published", True))

    result = await kotx_discard.discard_run_for_input(object(), raw.id)

    assert result is True
    assert calls["kotx_task_id"] == 42  # from _kotx_task fixture id
    assert calls["published"] is True


@pytest.mark.asyncio
async def test_discard_run_for_input_rejects_non_kotx_input(monkeypatch):
    raw = SimpleNamespace(id=uuid.uuid4(), source="gmail", source_metadata={})
    monkeypatch.setattr(kotx_discard.raw_inputs_store, "get", lambda s, i: raw)
    with pytest.raises(LookupError):
        await kotx_discard.discard_run_for_input(object(), raw.id)


def test_label_for_repo_prefers_config_match(monkeypatch):
    monkeypatch.setattr(
        kotx_runner, "load_labels", lambda: {"Uni": None, "CSEE": None, "SocialAI": None}
    )
    assert kotx_runner._label_for_repo("askLio/CSEE-strategic-negotiation-agent") == "CSEE"
    # Alphanumeric-only comparison bridges hyphenated org names.
    assert kotx_runner._label_for_repo("TUM-Social-AI/AflaConnect") == "SocialAI"
    assert kotx_runner._label_for_repo("owner/repo") is None
