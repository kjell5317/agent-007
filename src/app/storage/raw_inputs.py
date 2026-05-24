from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import String, bindparam, select, text
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


def set_embedding(
    session: Session, raw_input_id: uuid.UUID, embedding: list[float]
) -> None:
    row = session.get(RawInput, raw_input_id)
    if row is None:
        return
    row.embedding = embedding
    session.flush()


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


# --- Similar-input lookup -----------------------------------------------------


@dataclass
class SimilarInput:
    """One past raw input ranked by cosine similarity to a query embedding."""

    id: uuid.UUID
    source: str
    status: str
    similarity: float  # 1 - cosine_distance, in [0, 1] (clamped negative→0)
    agent_trace: dict | None
    subject: str | None
    sender: str | None
    received_at: datetime


# pgvector's `<=>` returns cosine *distance* (0..2). Similarity = 1 - distance.
# We pull only processed/skipped rows (decisions exist) and exclude the row
# we're currently classifying.
_SIMILAR_INPUTS_SQL = text(
    """
    SELECT
      id,
      source,
      status,
      received_at,
      agent_trace,
      source_metadata,
      1.0 - (embedding <=> CAST(:emb AS vector)) AS similarity
    FROM raw_inputs
    WHERE embedding IS NOT NULL
      AND processed_at IS NOT NULL
      AND status IN ('processed', 'skipped')
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
    k: int = 5,
) -> list[SimilarInput]:
    """Top-k past raw inputs by cosine similarity to `embedding`."""
    emb_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    rows = session.execute(
        _SIMILAR_INPUTS_SQL,
        {"emb": emb_literal, "exclude_id": exclude_id, "k": k},
    ).all()
    out: list[SimilarInput] = []
    for r in rows:
        meta = r.source_metadata or {}
        out.append(
            SimilarInput(
                id=r.id,
                source=r.source,
                status=r.status,
                similarity=float(r.similarity) if r.similarity is not None else 0.0,
                agent_trace=r.agent_trace,
                subject=meta.get("subject"),
                sender=meta.get("from"),
                received_at=r.received_at,
            )
        )
    return out
