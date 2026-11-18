"""Event notifications — they fire once, at a moment. Keyed ones (a new task,
a start cue) go through the ledger, so a re-run never sends them twice and
closing the task clears them with its other pushes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.clients import notification_ledger as ledger
from app.services.notifications import messages, push
from app.services.notifications.push import Message


async def notify_error(title: str, exc: BaseException, *, context: str | None = None) -> None:
    await push.send(messages.error(title, exc, context=context))


async def notify_new_task(session: Session, task) -> None:
    await send_once(
        session, key=f"created:{task.id}", kind="new_task",
        message=messages.new_task(task), task_id=task.id,
    )


async def notify_done_for_you(task, *, headline: str, text: str | None, reopen: bool = True) -> None:
    await push.send(messages.done_for_you(task, headline=headline, text=text, reopen=reopen))


async def notify_draft_ready(task, *, mail_url: str, note: str | None = None) -> None:
    await push.send(messages.draft_ready(task, mail_url=mail_url, note=note))


async def notify_draft_stale(task, *, mail_url: str) -> None:
    await push.send(messages.draft_stale(task, mail_url=mail_url))


async def send_once(
    session: Session,
    *,
    key: str,
    kind: str,
    message: Message,
    task_id: uuid.UUID | None = None,
    fingerprint: str = "",
) -> bool:
    if ledger.get(session, key) is not None:
        return False
    await push.send(message)
    ledger.record_sent(
        session, key=key, kind=kind, tag=message.tag or "", fingerprint=fingerprint,
        task_id=task_id, now=datetime.now(timezone.utc),
    )
    session.commit()
    return True


async def clear_task_notifications(
    session: Session, task_id: uuid.UUID, *, kinds: set[str] | None = None
) -> None:
    """Remove the task's pushes that are still showing — all of them, or only
    those of `kinds`."""
    now = datetime.now(timezone.utc)
    rows = [
        row for row in ledger.showing_for_task(session, task_id)
        if kinds is None or row.kind in kinds
    ]
    for row in rows:
        await push.clear(row.tag)
        ledger.record_cleared(session, row, now)
    if rows:
        session.commit()
