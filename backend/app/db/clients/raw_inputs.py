from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import String, bindparam, func, select, text
from sqlalchemy.orm import Session

from app.db.models.raw_input import RawInput
from app.db.schemas.raw_input import RawInputCreate


def create(session: Session, payload: RawInputCreate) -> RawInput:
    """Insert a raw input; on (source, external_id) conflict return the existing row."""
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


def list_(
    session: Session,
    *,
    status: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[RawInput]:
    stmt = select(RawInput).order_by(RawInput.received_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(RawInput.status == status)
    if source is not None:
        stmt = stmt.where(RawInput.source == source)
    return list(session.execute(stmt).scalars())


def count_since(session: Session, ts: datetime) -> int:
    """Count raw inputs received after `ts` for the unread badge.

    Manual entries are excluded — the user just created them via POST /tasks,
    they don't need a notification badge about their own creation. The inbox
    feed (list_) does still surface them so they can be reopened later.
    """
    stmt = (
        select(func.count(RawInput.id))
        .where(RawInput.received_at > ts, RawInput.source != "manual")
    )
    return int(session.execute(stmt).scalar_one() or 0)


def set_embedding(
    session: Session, raw_input_id: uuid.UUID, embedding: list[float]
) -> None:
    row = session.get(RawInput, raw_input_id)
    if row is None:
        return
    row.embedding = embedding
    session.flush()


def finalize(
    session: Session,
    raw_input_id: uuid.UUID,
    *,
    status: str,
    task_id: uuid.UUID | None = None,
    agent_trace: dict | None = None,
) -> RawInput | None:
    """Mark a raw input as processed with its final status + (optional) task link."""
    row = session.get(RawInput, raw_input_id)
    if row is None:
        return None
    row.status = status
    row.processed_at = datetime.now(timezone.utc)
    if task_id is not None:
        row.task_id = task_id
    if agent_trace is not None:
        row.agent_trace = agent_trace
    session.flush()
    return row


def latest_for_task(session: Session, task_id: uuid.UUID) -> RawInput | None:
    """Return the most-recent *anchor* raw_input linked to a task — the row
    whose status defines the task's current derived state (see
    `latest_status_for`). Duplicates are references back to the task, not
    state transitions for it, so they are skipped."""
    stmt = (
        select(RawInput)
        .where(RawInput.task_id == task_id, RawInput.status != "duplicate")
        .order_by(RawInput.received_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def find_by_thread(
    session: Session, source: str, thread_id: str
) -> RawInput | None:
    """Return the most-recent raw_input on (source, thread_id) that linked to a task."""
    stmt = (
        select(RawInput)
        .where(
            RawInput.source == source,
            RawInput.task_id.is_not(None),
            text("source_metadata->>'thread_id' = :thread_id"),
        )
        .order_by(RawInput.received_at.desc())
        .limit(1)
        .params(thread_id=thread_id)
    )
    return session.execute(stmt).scalar_one_or_none()


# --- Similarity search --------------------------------------------------------


@dataclass
class SimilarInput:
    id: uuid.UUID
    source: str
    status: str
    task_id: uuid.UUID | None
    similarity: float
    agent_trace: dict | None
    subject: str | None
    sender: str | None
    received_at: datetime


_SIMILAR_INPUTS_SQL = text(
    """
    SELECT
      id, source, status, task_id, received_at, agent_trace, source_metadata,
      1.0 - (embedding <=> CAST(:emb AS vector)) AS similarity
    FROM raw_inputs
    WHERE embedding IS NOT NULL
      AND processed_at IS NOT NULL
      AND status = ANY(:statuses)
      AND id <> :exclude_id
    ORDER BY embedding <=> CAST(:emb AS vector)
    LIMIT :k
    """
).bindparams(bindparam("emb", type_=String()))


def search_similar(
    session: Session,
    *,
    embedding: list[float],
    exclude_id: uuid.UUID,
    statuses: list[str],
    k: int = 5,
) -> list[SimilarInput]:
    """Top-k past raw inputs by cosine similarity, filtered to the given statuses."""
    emb_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    rows = session.execute(
        _SIMILAR_INPUTS_SQL,
        {
            "emb": emb_literal,
            "exclude_id": exclude_id,
            "statuses": statuses,
            "k": k,
        },
    ).all()
    out: list[SimilarInput] = []
    for r in rows:
        meta = r.source_metadata or {}
        out.append(
            SimilarInput(
                id=r.id,
                source=r.source,
                status=r.status,
                task_id=r.task_id,
                similarity=float(r.similarity) if r.similarity is not None else 0.0,
                agent_trace=r.agent_trace,
                subject=meta.get("subject"),
                sender=meta.get("from"),
                received_at=r.received_at,
            )
        )
    return out
