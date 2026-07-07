from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, bindparam, select, text
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.db.models.note import Note


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
    source_from: str | None
    source_subject: str | None


# Hybrid: fuse a pgvector nearest-neighbour ranking with a Postgres FTS ranking
# via Reciprocal Rank Fusion (Σ 1/(60+rank)), so a note that matches by keyword
# (an account number, a name) surfaces even when the embedding misses it, and
# vice versa. RRF is rank-based, so cosine and ts_rank scales never need
# reconciling. The fused score is then re-ranked by the same recency decay the
# vector-only lookup used, keeping the mild bias toward recent memory.
_NOTE_POOL = 40

_SIMILAR_NOTES_SQL = text(
    """
    WITH q AS (SELECT websearch_to_tsquery('english', :raw_q) AS tsq),
    vec AS (
        SELECT n.id, row_number() OVER (ORDER BY n.embedding <=> CAST(:emb AS vector)) AS rnk
        FROM notes n
        WHERE n.embedding IS NOT NULL
        ORDER BY n.embedding <=> CAST(:emb AS vector)
        LIMIT :pool
    ),
    kw AS (
        SELECT n.id, row_number() OVER (ORDER BY ts_rank_cd(n.tsv, q.tsq) DESC) AS rnk
        FROM notes n, q
        WHERE q.tsq @@ n.tsv
        ORDER BY ts_rank_cd(n.tsv, q.tsq) DESC
        LIMIT :pool
    ),
    fused AS (
        SELECT coalesce(vec.id, kw.id) AS id,
               coalesce(1.0 / (60 + vec.rnk), 0.0)
                 + coalesce(1.0 / (60 + kw.rnk), 0.0) AS rrf
        FROM vec FULL OUTER JOIN kw ON vec.id = kw.id
    )
    SELECT
      n.id, n.content, n.source_raw_input_id, n.created_at,
      r.source_metadata->>'from' AS source_from,
      r.source_metadata->>'subject' AS source_subject,
      1.0 - (n.embedding <=> CAST(:emb AS vector)) AS similarity
    FROM fused
    JOIN notes n ON n.id = fused.id
    LEFT JOIN raw_inputs r ON r.id = n.source_raw_input_id
    ORDER BY
      fused.rrf
        * exp(- EXTRACT(EPOCH FROM (now() - n.created_at)) / (:half_life_days * 86400.0))
      DESC
    LIMIT :k
    """
).bindparams(bindparam("emb", type_=String()), bindparam("raw_q", type_=String()))


def search_similar(
    session: Session, *, embedding: list[float], query: str, k: int = 5
) -> list[SimilarNote]:
    """Top-k notes by hybrid similarity + keyword (RRF over pgvector and FTS),
    re-ranked with a mild recency decay."""
    emb_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    half_life_days = get_settings().notes_similarity_half_life_days
    rows = session.execute(
        _SIMILAR_NOTES_SQL,
        {
            "emb": emb_literal,
            "raw_q": query or "",
            "k": k,
            "pool": _NOTE_POOL,
            "half_life_days": half_life_days,
        },
    ).all()
    return [
        SimilarNote(
            id=r.id,
            content=r.content,
            similarity=float(r.similarity) if r.similarity is not None else 0.0,
            source_raw_input_id=r.source_raw_input_id,
            created_at=r.created_at,
            source_from=r.source_from,
            source_subject=r.source_subject,
        )
        for r in rows
    ]


def list_recent(session: Session, *, limit: int = 20) -> list[Note]:
    stmt = select(Note).order_by(Note.created_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars())
