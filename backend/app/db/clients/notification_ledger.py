from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.notification import SentNotification


def get(session: Session, key: str) -> SentNotification | None:
    return session.get(SentNotification, key)


def of_kinds(session: Session, kinds: Iterable[str]) -> list[SentNotification]:
    stmt = select(SentNotification).where(SentNotification.kind.in_(list(kinds)))
    return list(session.execute(stmt).scalars())


def showing_of_kind(session: Session, kind: str) -> list[SentNotification]:
    stmt = select(SentNotification).where(
        SentNotification.kind == kind, SentNotification.cleared_at.is_(None)
    )
    return list(session.execute(stmt).scalars())


def showing_for_task(session: Session, task_id: uuid.UUID) -> list[SentNotification]:
    stmt = select(SentNotification).where(
        SentNotification.task_id == task_id, SentNotification.cleared_at.is_(None)
    )
    return list(session.execute(stmt).scalars())


def record_sent(
    session: Session,
    *,
    key: str,
    kind: str,
    tag: str,
    fingerprint: str,
    now: datetime,
    task_id: uuid.UUID | None = None,
) -> SentNotification:
    row = get(session, key)
    if row is None:
        row = SentNotification(key=key, kind=kind)
        session.add(row)
    row.kind = kind
    row.tag = tag
    row.fingerprint = fingerprint
    row.task_id = task_id
    row.sent_at = now
    row.cleared_at = None
    session.flush()
    return row


def record_cleared(session: Session, row: SentNotification, now: datetime) -> None:
    row.cleared_at = now
    session.flush()
