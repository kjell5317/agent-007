"""Start and run agent hand-offs (docs/handoff-plan.md).

`start` is called right after the extraction agent's task is committed.
Research, lookups and drafts run at once in the background; a code change
waits for the user's tap (`accept`). Each executor returns `(state, result)`;
a failure marks the hand-off failed and leaves the task untouched.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.db.clients import handoffs as handoffs_store
from app.db.clients import raw_inputs as raw_inputs_store
from app.db.clients import tasks as tasks_store
from app.db.models.handoff import TaskHandoff
from app.events import publish_task
from app.services.handoff.eligible import AUTO_KINDS, HANDOFF_KINDS, valid_handoff
from app.services.handoff.text import append_section, sources_markdown
from app.services.notify import (
    clear_notification_tag,
    handoff_tag,
    notify_error,
    notify_handoff_proposed,
    notify_research_moved_due,
)

log = logging.getLogger(__name__)

Executor = Callable[[Session, Any, TaskHandoff], Awaitable[tuple[str, dict[str, Any]]]]

# Strong references: the loop only keeps weak ones to running tasks.
_running: set[asyncio.Task] = set()


async def start(session: Session, task_id: uuid.UUID, choice: dict[str, str]) -> None:
    if handoffs_store.active_for_task(session, task_id) is not None:
        return
    auto = choice["kind"] in AUTO_KINDS
    row = handoffs_store.create(
        session, task_id=task_id, kind=choice["kind"], brief=choice["brief"],
        url=choice.get("url"), state="running" if auto else "proposed",
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
    if kind in ("email_reply", "code_change"):
        raw = raw_inputs_store.latest_for_task(session, task.id)
        choice = {"kind": kind, "brief": brief}
        if raw is None or valid_handoff(choice, raw, label=task.label, link=task.link) is None:
            raise ValueError(_unavailable(kind))
    row = handoffs_store.create(
        session, task_id=task.id, kind=kind, brief=brief, url=None, state="running",
    )
    session.commit()
    publish_task(session, task.id)
    _spawn(row.id)
    return row


def _unavailable(kind: str) -> str:
    if kind == "email_reply":
        return "Drafts need a task from an email that accepts replies"
    if kind == "code_change":
        return "Issues can only be opened for kotx-labelled tasks without a GitHub link"
    return "This hand-off isn't available for this task"


async def accept(session: Session, task) -> TaskHandoff:
    row = handoffs_store.active_for_task(session, task.id)
    if row is None or row.state != "proposed":
        raise LookupError("No proposed hand-off on this task")
    row.state = "running"
    session.commit()
    publish_task(session, task.id)
    await clear_notification_tag(handoff_tag(task.id))
    _spawn(row.id)
    return row


async def decline(session: Session, task) -> None:
    row = handoffs_store.active_for_task(session, task.id)
    if row is None or row.state != "proposed":
        raise LookupError("No proposed hand-off on this task")
    handoffs_store.finish(session, row, "declined")
    session.commit()
    publish_task(session, task.id)
    await clear_notification_tag(handoff_tag(task.id))


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
            state, result = await _EXECUTORS[row.kind](session, task, row)
        except Exception as exc:  # noqa: BLE001 — a failed hand-off must not touch the task
            log.exception("hand-off failed · kind=%s task=%s", row.kind, row.task_id)
            session.rollback()
            row = handoffs_store.get(session, handoff_id)
            handoffs_store.finish(session, row, "failed", {"error": str(exc)[:500]})
            session.commit()
            publish_task(session, row.task_id)
            await notify_error(f"Agent hand-off failed ({row.kind})", exc, context=task.title)
            return
        handoffs_store.finish(session, row, state, result)
        session.commit()
        publish_task(session, task.id)


async def _code_change(session: Session, task, row: TaskHandoff) -> tuple[str, dict[str, Any]]:
    from app.services.kotx import create_issue_run

    body = None
    if not (task.description or "").strip():
        raw = raw_inputs_store.latest_for_task(session, task.id)
        body = "\n\n".join(filter(None, [row.brief, (raw.content or "").strip() if raw else ""]))
    run = await create_issue_run(task, body=body)
    task.link = run.issue_url
    return "ready", {"issue_url": run.issue_url}


async def _web_research(session: Session, task, row: TaskHandoff) -> tuple[str, dict[str, Any]]:
    from app.agent.handoff.research import correct_fields
    from app.agent.helpers.web import research
    from app.services.task.update import update_task

    found = await research(
        get_settings(),
        url=row.url,
        query=None if row.url else row.brief,
        context=f"{task.title}. {row.brief}",
    )
    if not found.ok:
        raise RuntimeError(f"Web research failed: {found.error}")
    section = found.summary + "\n\n" + sources_markdown(found.sources)
    corrections = await correct_fields(task, found.summary)
    fields: dict[str, Any] = {"description": append_section(task.description, "Research", section)}
    if corrections.get("due_date") and corrections["due_date"] != task.due_date and not task.parent_task_id:
        fields["due_date"] = corrections["due_date"]
    if corrections.get("location") and corrections["location"] != task.location:
        fields["location"] = corrections["location"]
    await update_task(session, task.id, fields)
    if "due_date" in fields:
        await notify_research_moved_due(task)
    return "ready", {
        "summary": found.summary,
        "sources": [{"title": t, "url": u} for t, u in found.sources],
        "queries": found.queries,
        "changed": sorted(fields),
    }


async def _own_data_lookup(session: Session, task, row: TaskHandoff) -> tuple[str, dict[str, Any]]:
    from app.agent.chat.runner import answer_task
    from app.services.task.update import update_task

    answer = await answer_task(session, task, row.brief)
    if not answer:
        raise RuntimeError("The lookup returned no answer")
    await update_task(
        session, task.id, {"description": append_section(task.description, "Agent notes", answer)},
    )
    return "ready", {"summary": answer}


async def _email_reply(session: Session, task, row: TaskHandoff) -> tuple[str, dict[str, Any]]:
    from app.services.email.draft import create_reply_draft, draft_mail_url

    draft = await create_reply_draft(session, task, brief=row.brief)
    return "ready", {
        "draft_id": str(draft.id),
        "from": draft.from_address,
        "open_questions": draft.open_questions,
        "mail_url": draft_mail_url(session, draft),
    }


_EXECUTORS: dict[str, Executor] = {
    "code_change": _code_change,
    "web_research": _web_research,
    "own_data_lookup": _own_data_lookup,
    "email_reply": _email_reply,
}
