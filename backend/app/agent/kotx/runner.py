"""Deterministic handler for kotx state transitions.

kotx transition semantics are fixed, so — matching the auto-branch
philosophy — no LLM decides here. The one exception: when an actionable
transition creates a new 007 task, the extract-fields agent runs over the
brief (TASK.md / REVIEW.md) to set estimation and due date.

Task matching order: kotx_task_id → github thread precedent → github link
on an existing task (the "007 task created the issue" adoption path).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agent.manual.runner import extract_task_fields
from app.db.clients import raw_inputs, tasks
from app.db.models.task import Task
from app.db.schemas.task import TaskCreate
from app.services.input.kotx.normalize import ACTIONABLE, DONE_STATES, parse_github_subject
from app.services.plan import schedule_task
from app.services.task.close import close_task
from app.services.task.reopen import reopen_task

log = logging.getLogger(__name__)


async def run_kotx_transition(session: Session, raw) -> dict:
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
        await close_task(session, task.id, discard_kotx=False)
        trace["outcome"] = "closed"
    else:
        trace["outcome"] = "no_change"

    raw_inputs.finalize(
        session, raw.id, status="duplicate", task_id=task.id, agent_trace=trace
    )
    session.commit()
    return trace


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

    if repo and number:
        for candidate in tasks.github_link_candidates(session, repo, number):
            if parse_github_subject(candidate.link or "") == (repo, number):
                return candidate, "github_link"
    return None, None


async def _create_task_from_brief(
    session: Session, raw, meta: dict, kotx_id: int
) -> Task:
    payload = await extract_task_fields(session, raw)
    task = tasks.create(
        session,
        TaskCreate(
            title=str(payload.get("title") or meta.get("subject") or "kotx task"),
            description=str(payload.get("description")) if payload.get("description") else None,
            estimation=payload.get("estimation"),
            due_date=payload.get("due_date"),
            location=str(payload.get("location")) if payload.get("location") else None,
            link=str(meta.get("github_url") or payload.get("link") or "") or None,
            label=str(payload.get("label")) if payload.get("label") else None,
        ),
    )
    task.kotx_task_id = kotx_id
    session.flush()
    await schedule_task(session, task)
    return task
