from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, bindparam, select, text
from sqlalchemy.orm import Session

from app.models.note import Note


def create(
    session: Session,
    *,
    content: str,
    source_raw_input_id: uuid.UUID | None,
    embedding: list[float] | None,
) -> Note:
    row = Note(
        content=content,
        source_raw_input_id=source_raw_input_id,
        embedding=embedding,
    )
    session.add(row)
    session.flush()
    return row


@dataclass
class SimilarNote:
    id: uuid.UUID
    content: str
    similarity: float
    source_raw_input_id: uuid.UUID | None
    created_at: datetime


_SIMILAR_NOTES_SQL = text(
    """
    SELECT
      id, content, source_raw_input_id, created_at,
      1.0 - (embedding <=> CAST(:emb AS vector)) AS similarity
    FROM notes
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> CAST(:emb AS vector)
    LIMIT :k
    """
).bindparams(bindparam("emb", type_=String()))


def search_similar(
    session: Session, *, embedding: list[float], k: int = 5
) -> list[SimilarNote]:
    """Top-k notes by cosine similarity."""
    emb_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    rows = session.execute(_SIMILAR_NOTES_SQL, {"emb": emb_literal, "k": k}).all()
    return [
        SimilarNote(
            id=r.id,
            content=r.content,
            similarity=float(r.similarity) if r.similarity is not None else 0.0,
            source_raw_input_id=r.source_raw_input_id,
            created_at=r.created_at,
        )
        for r in rows
    ]


def list_recent(session: Session, *, limit: int = 20) -> list[Note]:
    stmt = select(Note).order_by(Note.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars())
