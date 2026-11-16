"""Reply drafts 007 created in a mailbox."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.email_draft import EmailDraft

LIVE_STATES = ("generated", "pushed", "stale")


def create(session: Session, **fields) -> EmailDraft:
    row = EmailDraft(**fields)
    session.add(row)
    session.flush()
    return row


def get(session: Session, draft_id: uuid.UUID) -> EmailDraft | None:
    return session.get(EmailDraft, draft_id)


def live_for_task(session: Session, task_id: uuid.UUID) -> EmailDraft | None:
    return session.scalars(
        select(EmailDraft)
        .where(EmailDraft.task_id == task_id, EmailDraft.state.in_(LIVE_STATES))
        .order_by(EmailDraft.created_at.desc())
        .limit(1)
    ).first()


def live_for_threads(session: Session, thread_ids: Iterable[str]) -> list[EmailDraft]:
    ids = [t for t in set(thread_ids) if t]
    if not ids:
        return []
    return list(session.scalars(
        select(EmailDraft).where(
            EmailDraft.provider_thread_ref.in_(ids), EmailDraft.state.in_(("pushed", "stale"))
        )
    ))
