"""Start and run agent hand-offs.

`start` is called right after the extraction agent's task is committed.
Research and drafts run at once in the background; a code change waits for
the user's tap (`accept`). Each executor returns `(state, result, trace)`;
a failure marks the hand-off failed and leaves the task untouched.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.clients import handoffs as handoffs_store
from app.db.clients import labels as labels_store
from app.db.clients import raw_inputs as raw_inputs_store
from app.db.clients import tasks as tasks_store
from app.db.models.handoff import TaskHandoff
from app.events import publish_task
from app.services.handoff.eligible import (
    AUTO_KINDS,
    HANDOFF_KINDS,
    code_change_possible,
    replyable,
)
from app.services.handoff.text import append_section
from app.services.kotx.notify import notify_handoff_proposed
from app.services.notifications import push
from app.services.notifications.messages import handoff_tag
from app.services.notifications.notify import notify_done_for_you, notify_error, notify_new_task

log = logging.getLogger(__name__)

Outcome = tuple[str, dict[str, Any], dict[str, Any] | None]
Executor = Callable[[Session, Any, TaskHandoff], Awaitable[Outcome]]

# Strong references: the loop only keeps weak ones to running tasks.
_running: set[asyncio.Task] = set()


async def start(session: Session, task_id: uuid.UUID, choice: dict[str, str]) -> None:
    if handoffs_store.active_for_task(session, task_id) is not None:
        return
    auto = choice["kind"] in AUTO_KINDS
    row = handoffs_store.create(
        session, task_id=task_id, kind=choice["kind"], brief=choice["brief"],
        state="running" if auto else "proposed",
    )
    session.commit()
    publish_task(session, task_id)
    if auto:
        _spawn(row.id)
    else:
        await notify_handoff_proposed(tasks_store.get(session, task_id), brief=row.brief)


async def start_manual(session: Session, task, kind: str, brief: str | None) -> TaskHandoff:
    """The "Hand off…" menu. The tap is the approval, so a code change runs too."""
    if kind not in HANDOFF_KINDS:
        raise ValueError(f"Unknown hand-off kind: {kind}")
    if handoffs_store.active_for_task(session, task.id) is not None:
        raise ValueError("An agent is already working on this task")
    brief = " ".join((brief or task.title).split())[:500]
    if kind == "email_reply":
        raw = raw_inputs_store.latest_for_task(session, task.id)
        if raw is None or not replyable(raw):
            raise ValueError("Drafts need a task from an email that accepts replies")
    if kind == "code_change" and not code_change_possible(
        task.label, task.link, labels_store.repos_by_name(session),
    ):
        raise ValueError("Issues need a label connected to a GitHub repo and no GitHub link yet")
    row = handoffs_store.create(session, task_id=task.id, kind=kind, brief=brief, state="running")
    session.commit()
    publish_task(session, task.id)
    _spawn(row.id)
    return row


async def accept(session: Session, task) -> TaskHandoff:
    row = handoffs_store.active_for_task(session, task.id)
    if row is None or row.state != "proposed":
        raise LookupError("No proposed hand-off on this task")
    row.state = "running"
    session.commit()
    publish_task(session, task.id)
    await push.clear(handoff_tag(task.id))
    _spawn(row.id)
    return row


async def decline(session: Session, task) -> None:
    row = handoffs_store.active_for_task(session, task.id)
    if row is None or row.state != "proposed":
        raise LookupError("No proposed hand-off on this task")
    handoffs_store.finish(session, row, "declined")
    session.commit()
    publish_task(session, task.id)
    await push.clear(handoff_tag(task.id))


def _spawn(handoff_id: uuid.UUID) -> None:
    job = asyncio.get_running_loop().create_task(execute(handoff_id), name=f"handoff-{handoff_id}")
    _running.add(job)
    job.add_done_callback(_running.discard)


async def execute(handoff_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        row = handoffs_store.get(session, handoff_id)
        task = tasks_store.get(session, row.task_id) if row is not None else None
        if row is None or task is None:
            return
        try:
            state, result, trace = await _EXECUTORS[row.kind](session, task, row)
        except Exception as exc:  # noqa: BLE001 — a failed hand-off must not touch the task
            log.exception("hand-off failed · kind=%s task=%s", row.kind, row.task_id)
            session.rollback()
            row = handoffs_store.get(session, handoff_id)
            handoffs_store.finish(session, row, "failed", {"error": str(exc)[:500]})
            session.commit()
            publish_task(session, row.task_id)
            await notify_error(f"Agent hand-off failed ({row.kind})", exc, context=task.title)
            await _announce_unfinished(session, task)
            return
        handoffs_store.finish(session, row, state, result, trace)
        session.commit()
        publish_task(session, task.id)


async def _code_change(session: Session, task, row: TaskHandoff) -> Outcome:
    from app.services.kotx import create_issue_run

    repo = labels_store.repos_by_name(session).get(task.label or "")
    if not repo:
        raise RuntimeError("The task's label isn't connected to a GitHub repo")
    body = None
    if not (task.description or "").strip():
        raw = raw_inputs_store.latest_for_task(session, task.id)
        body = "\n\n".join(filter(None, [row.brief, (raw.content or "").strip() if raw else ""]))
    run = await create_issue_run(task, repo=repo, body=body)
    task.link = run.issue_url
    return "ready", {"issue_url": run.issue_url}, None


async def _research(session: Session, task, row: TaskHandoff) -> Outcome:
    from app.agent.handoff.research import run_research
    from app.agent.helpers.web import task_urls
    from app.services.task.close import close_task
    from app.services.task.update import update_task

    raw = raw_inputs_store.latest_for_task(session, task.id)
    report = await run_research(session, task, row.brief, task_urls(raw) if raw is not None else [])
    fields: dict[str, Any] = {
        "description": append_section(task.description, "Research", report.findings),
    }
    # A completed task closes, so a new deadline would only reschedule it first.
    if (
        not report.completes_task and report.due_date and report.due_date != task.due_date
        and not task.parent_task_id
    ):
        fields["due_date"] = report.due_date
    if report.location and report.location != task.location:
        fields["location"] = report.location
    await update_task(session, task.id, fields)
    if report.completes_task:
        # The agent did the work, so no completion points.
        await close_task(session, task.id, award_points=False)
        await notify_done_for_you(task, headline="Answered", text=report.findings)
    else:
        session.refresh(task)
        await _announce_unfinished(session, task)
    return "ready", {
        "summary": report.findings,
        "completed": report.completes_task,
        "changed": sorted(fields),
    }, report.trace


async def _email_reply(session: Session, task, row: TaskHandoff) -> Outcome:
    from app.services.email.draft import create_reply_draft, draft_mail_url

    draft, trace = await create_reply_draft(session, task, brief=row.brief)
    return "ready", {
        "draft_id": str(draft.id),
        "from": draft.from_address,
        "open_questions": draft.open_questions,
        "mail_url": draft_mail_url(session, draft),
    }, trace


async def _announce_unfinished(session: Session, task) -> None:
    """An agent-created task whose hand-off couldn't finish it is now the
    user's: announce it like a task created without one. A typed-in task is
    already known."""
    if not tasks_store.created_manually(session, task):
        await notify_new_task(session, task)


_EXECUTORS: dict[str, Executor] = {
    "research": _research,
    "email_reply": _email_reply,
    "code_change": _code_change,
}
