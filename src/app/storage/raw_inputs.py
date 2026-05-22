from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.raw_input import RawInput
from app.schemas.raw_input import RawInputCreate


def create(session: Session, payload: RawInputCreate) -> RawInput:
    """Insert a raw input; on (source, external_id) conflict return the existing row.

    Idempotent so pollers can re-fetch a message id without producing dupes.
    """
    if payload.external_id is not None:
        existing = session.execute(
            select(RawInput).where(
                RawInput.source == payload.source,
                RawInput.external_id == payload.external_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    row = RawInput(
        source=payload.source,
        external_id=payload.external_id,
        content=payload.content,
        source_metadata=payload.source_metadata,
    )
    session.add(row)
    session.flush()
    return row


def get(session: Session, raw_input_id: uuid.UUID) -> RawInput | None:
    return session.get(RawInput, raw_input_id)


def mark_processed(
    session: Session,
    raw_input_id: uuid.UUID,
    *,
    status: str,
    agent_trace: dict | None = None,
) -> None:
    row = session.get(RawInput, raw_input_id)
    if row is None:
        return
    row.status = status
    row.processed_at = datetime.now(timezone.utc)
    if agent_trace is not None:
        row.agent_trace = agent_trace
    session.flush()
