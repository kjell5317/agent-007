"""CRUD + semantic lookup for the chat answer cache.

Matched question→question by cosine only (no keyword side): a repeated or
paraphrased *question* is what we want to recognise, and question words ("what",
"is", "when") make FTS noisy here — unlike notes, which key on content. Bounded
by a recency window so a stale answer can't resurface long after the facts moved.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Float, Integer, String, bindparam, text
from sqlalchemy.orm import Session

from app.db.models.chat_answer import ChatAnswer


def create(
    session: Session, *, question: str, answer: str, embedding: list[float] | None
) -> ChatAnswer:
    row = ChatAnswer(question=question, answer=answer, embedding=embedding)
    session.add(row)
    session.flush()
    return row


@dataclass
class SimilarAnswer:
    id: uuid.UUID
    question: str
    answer: str
    similarity: float
    created_at: datetime


_SIMILAR_SQL = text(
    """
    SELECT id, question, answer, created_at,
           1.0 - (embedding <=> CAST(:emb AS vector)) AS similarity
    FROM chat_answers
    WHERE embedding IS NOT NULL
      AND created_at >= now() - make_interval(days => :max_age_days)
      AND (1.0 - (embedding <=> CAST(:emb AS vector))) >= :min_sim
    ORDER BY embedding <=> CAST(:emb AS vector)
    LIMIT :k
    """
).bindparams(
    bindparam("emb", type_=String()),
    bindparam("min_sim", type_=Float()),
    bindparam("max_age_days", type_=Integer()),
)


def search_similar(
    session: Session,
    *,
    embedding: list[float],
    k: int = 1,
    min_similarity: float,
    max_age_days: int,
) -> list[SimilarAnswer]:
    """Nearest recent cached answers whose question clears `min_similarity`."""
    emb_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    rows = session.execute(
        _SIMILAR_SQL,
        {"emb": emb_literal, "min_sim": min_similarity, "max_age_days": max_age_days, "k": k},
    ).all()
    return [
        SimilarAnswer(
            id=r.id,
            question=r.question,
            answer=r.answer,
            similarity=float(r.similarity) if r.similarity is not None else 0.0,
            created_at=r.created_at,
        )
        for r in rows
    ]
