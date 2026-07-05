"""Deterministic handler for kotx state transitions.

kotx transition semantics are fixed, so — matching the auto-branch
philosophy — no LLM decides here. The one exception: when an actionable
transition creates a new 007 task, the extract-fields agent runs over the
brief (TASK.md / REVIEW.md) to set estimation and due date. Everything else
is deterministic: title comes from the github subject (minus the repo,
which the label already carries), the label from a repo ↔ label-name
match (the agent's pick is only a fallback), and
description/location/link stay empty — the kotx run section carries that
context in the frontend.

Task matching order: kotx_task_id → github thread precedent → github link
on an existing task (the "007 task created the issue" adoption path).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.agent.manual.runner import extract_task_fields
from app.db.clients import raw_inputs, tasks
from app.labels import load_labels
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate
from app.services.input.kotx.normalize import ACTIONABLE, DONE_STATES, parse_github_subject
from app.services.kotx import client as kotx_client
from app.services.notify import (
    ACTION_KOTX_APPROVE,
    ACTION_KOTX_COMMENT,
    ACTION_KOTX_MERGE,
    ACTION_KOTX_START,
    clear_task_notification,
    notify_kotx_confirm_merge,
    notify_kotx_open_pr,
    notify_kotx_review_ready,
    notify_kotx_start,
)
from app.services.plan import schedule_task
from app.services.task.close import close_task
from app.services.task.reopen import reopen_task

log = logging.getLogger(__name__)


# Per-kotx-id locks serialize processing of the same kotx task. Keyed by id so
# transitions for different tasks still run concurrently; the set of live ids is
# small and bounded (personal use), so the dict never needs pruning.
_locks: dict[int, asyncio.Lock] = {}


def _lock_for(kotx_id: int) -> asyncio.Lock:
    # Safe on a single event loop: no await between the read and the insert.
    lock = _locks.get(kotx_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[kotx_id] = lock
    return lock


async def run_kotx_transition(session: Session, raw) -> dict:
    # One 007 task per kotx task. Concurrent transitions for the same kotx id —
    # overlapping webhook deliveries, or a webhook racing the reconciliation poll
    # — would otherwise both pass the get_by_kotx_id check (the LLM call in
    # _create_task_from_brief is the await that opens the window) and each create
    # a task, tripping uq_tasks_kotx_task_id. Serialize per id so the
    # check-then-create is atomic. Single process, so an in-process lock is
    # enough; the unique constraint stays as the backstop.
    kotx_id = int((raw.source_metadata or {})["kotx_task_id"])
    async with _lock_for(kotx_id):
        return await _run_transition(session, raw)


async def _run_transition(session: Session, raw) -> dict:
    meta = raw.source_metadata or {}
    kind = str(meta.get("kotx_kind") or "")
    state = str(meta.get("kotx_state") or "")
    kotx_id = int(meta["kotx_task_id"])
    repo = str(meta.get("repo") or "")
    number = int(meta.get("subject_number") or 0)

    trace: dict[str, Any] = {
        "outcome": None,
        "branch": "kotx",
        "auto_decided": True,
        "kotx_task_id": kotx_id,
        "kotx_state": state,
        "kotx_kind": kind,
    }

    task, matched_by = _match_task(session, meta, kotx_id, repo, number)
    if task is not None and task.kotx_task_id != kotx_id:
        task.kotx_task_id = kotx_id
        session.flush()
        trace["adopted"] = True
    if matched_by:
        trace["matched_by"] = matched_by

    actionable = (kind, state) in ACTIONABLE
    done = (kind, state) in DONE_STATES

    if actionable and task is None:
        task = await _create_task_from_brief(session, raw, meta, kotx_id)
        trace["outcome"] = "task_created"
        trace["task_id"] = str(task.id)
        trace["task_title"] = task.title
        raw_inputs.finalize(
            session, raw.id, status="open", task_id=task.id, agent_trace=trace
        )
        _backfill_unlinked_thread_inputs(session, meta, task, trace)
        session.commit()
        return trace

    if task is None:
        # Informational transition (drafting/queued/running/…) with no task
        # yet — keep it visible in the inbox, but it is not actionable.
        trace["outcome"] = "not_task"
        trace["reason"] = f"kotx {kind} is {state}; nothing to do yet"
        raw_inputs.finalize(session, raw.id, status="not_task", agent_trace=trace)
        session.commit()
        return trace

    status = tasks.latest_status_for(session, [task.id]).get(task.id, "open")
    trace["existing_task_id"] = str(task.id)

    if actionable and status != "open":
        try:
            await reopen_task(session, task.id)
            trace["outcome"] = "reopened"
        except LookupError:
            trace["outcome"] = "no_change"
    elif done and status == "open":
        # The kotx side already ended this run — never bounce a discard back.
        # A cancelled run isn't a completion, so it closes without the bonus.
        await close_task(
            session,
            task.id,
            discard_kotx=False,
            award_points=state != "cancelled",
        )
        trace["outcome"] = "closed"
    else:
        trace["outcome"] = "no_change"

    raw_inputs.finalize(
        session, raw.id, status="duplicate", task_id=task.id, agent_trace=trace
    )
    _backfill_unlinked_thread_inputs(session, meta, task, trace)
    session.commit()

    # kotx no longer sends its own approval/merge/review prompts (see docs). We
    # drive them here — but only on `no_change`. A `task_created`/`reopened`
    # transition already scheduled the task, which fired the "Scheduled"
    # notification; a start/review prompt on top of it would be the duplicate
    # the handoff warns against.
    if trace["outcome"] == "no_change":
        await _sync_kotx_prompt(task, kind, state, meta)
    return trace


async def _sync_kotx_prompt(task: Task, kind: str, state: str, meta: dict) -> None:
    """Post the prompt for the current transition, or clear a stale one.

    When the run moved past the state that proposed an action — the user opened
    the PR or merged on GitHub, or work simply resumed — the proposed action is
    no longer available, so any lingering prompt is dropped. resolve_conflict is
    auxiliary: it never prompts and never disturbs the primary run's prompt."""
    if kind == "implement" and state == "draft":
        await notify_kotx_start(task)
    elif kind == "implement" and state == "awaiting_approval":
        if meta.get("kotx_proposes") == "merge":
            ctx = await kotx_client.fetch_merge_context(int(meta["kotx_task_id"])) or {}
            await notify_kotx_confirm_merge(
                task,
                approved_by=ctx.get("approvedBy"),
                comment=ctx.get("commentMarkdown"),
            )
        else:
            await notify_kotx_open_pr(task)
    elif kind == "review" and state == "awaiting_approval":
        await notify_kotx_review_ready(task)
    elif kind in ("implement", "review"):
        await clear_task_notification(task.id)


def _kotx_primary_action(meta: dict) -> dict[str, str] | None:
    """The action button that replaces "Done" on a kotx task's first (scheduled)
    notification, chosen by the current transition."""
    kind = meta.get("kotx_kind")
    state = meta.get("kotx_state")
    if kind == "implement" and state == "draft":
        return {"action": ACTION_KOTX_START, "title": "Start"}
    if kind == "implement" and state == "awaiting_approval":
        if meta.get("kotx_proposes") == "merge":
            return {"action": ACTION_KOTX_MERGE, "title": "Merge"}
        return {"action": ACTION_KOTX_APPROVE, "title": "Open PR"}
    if kind == "review" and state == "awaiting_approval":
        return {"action": ACTION_KOTX_COMMENT, "title": "Comment"}
    return None


def _match_task(
    session: Session, meta: dict, kotx_id: int, repo: str, number: int
) -> tuple[Task | None, str | None]:
    task = tasks.get_by_kotx_id(session, kotx_id)
    if task is not None:
        return task, "kotx_task_id"

    thread_id = meta.get("thread_id")
    if thread_id:
        prior = raw_inputs.find_by_thread(session, None, str(thread_id))
        if prior is not None and prior.task_id is not None:
            linked = tasks.get(session, prior.task_id)
            if linked is not None:
                return linked, "github_thread"

    # A run anchored on the PR (a follow-up or resolve-conflict run) belongs
    # to the task whose issue-anchored run opened that PR. Without this, a
    # follow-up creates a second task for the same work — and awards a second
    # completion bonus when it closes.
    pr_number = meta.get("pr_number")
    if pr_number is None and meta.get("subject_type") == "pull_request":
        pr_number = meta.get("subject_number")
    if repo and pr_number is not None:
        prior = raw_inputs.find_kotx_by_pr(session, repo, int(pr_number))
        if prior is not None and prior.task_id is not None:
            linked = tasks.get(session, prior.task_id)
            if linked is not None:
                return linked, "github_pr"

    if repo and number:
        for candidate in tasks.github_link_candidates(session, repo, number):
            if parse_github_subject(candidate.link or "") == (repo, number):
                return candidate, "github_link"
    return None, None


def _backfill_unlinked_thread_inputs(
    session: Session, meta: dict, task: Task, trace: dict[str, Any]
) -> None:
    thread_id = meta.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.startswith("github:"):
        return

    linked = raw_inputs.link_unassigned_by_thread(
        session,
        source="kotx",
        thread_id=thread_id,
        task_id=task.id,
    )
    if linked:
        trace["backfilled_inputs"] = linked


async def _create_task_from_brief(
    session: Session, raw, meta: dict, kotx_id: int
) -> Task:
    payload = await extract_task_fields(session, raw)
    repo = str(meta.get("repo") or "")
    label = _label_for_repo(repo)
    if label is None and payload.get("label"):
        label = str(payload["label"])
    # The subject is "{repo}#{number} {title}" — the repo is already carried
    # by the label, so the task title drops it.
    title = str(meta.get("subject") or "kotx task").removeprefix(repo)
    task = tasks.create(
        session,
        TaskCreate(
            title=title,
            estimation=payload.get("estimation"),
            due_date=payload.get("due_date"),
            label=label,
        ),
    )
    task.kotx_task_id = kotx_id
    session.flush()
    # The first notification for this task is the normal "Scheduled" one, but
    # with its "Done" button swapped for the kotx action for this state.
    await schedule_task(session, task, primary_action=_kotx_primary_action(meta))
    return task


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _label_for_repo(repo: str) -> str | None:
    # Compare alphanumerics only so hyphenated org/repo names still match
    # (TUM-Social-AI → SocialAI).
    haystack = _squash(repo)
    return next((name for name in load_labels() if _squash(name) in haystack), None)
