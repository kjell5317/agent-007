"""CRUD + similarity search for the `documents` external-reference cache.

Providers (calendar, kotx, …) upsert here on `(provider, external_id)`; the
search layer and the agent's calendar tool read it. Embeddings are computed by
the caller (the provider's cache service) and stored as-is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, bindparam, delete as sa_delete, select, text
from sqlalchemy.orm import Session

from app.db.models.document import Document


def get_by_external_id(session: Session, *, provider: str, external_id: str) -> Document | None:
    return session.execute(
        select(Document).where(
            Document.provider == provider, Document.external_id == external_id
        )
    ).scalar_one_or_none()


def upsert(
    session: Session,
    *,
    provider: str,
    external_id: str,
    title: str,
    snippet: str | None,
    content: str | None,
    url: str | None,
    metadata: dict,
    starts_at: datetime | None,
    ends_at: datetime | None,
    updated_at: datetime,
    embedding: list[float] | None,
) -> Document:
    row = get_by_external_id(session, provider=provider, external_id=external_id)
    if row is None:
        row = Document(provider=provider, external_id=external_id)
        session.add(row)
    row.title = title or ""
    row.snippet = snippet
    row.content = content
    row.url = url
    row.meta = metadata or {}
    row.starts_at = starts_at
    row.ends_at = ends_at
    row.updated_at = updated_at
    row.embedding = embedding
    session.flush()
    return row


def delete(session: Session, *, provider: str, external_id: str) -> bool:
    result = session.execute(
        sa_delete(Document).where(
            Document.provider == provider, Document.external_id == external_id
        )
    )
    return bool(result.rowcount or 0)


@dataclass
class CalendarMatch:
    event_id: str
    calendar_id: str | None
    summary: str
    location: str | None
    starts_at: datetime | None
    similarity: float
    url: str | None = None


# Hybrid: fuse a pgvector nearest-neighbour ranking with a Postgres FTS ranking
# via Reciprocal Rank Fusion (score = Σ 1/(60+rank)). RRF is rank-based, so the
# cosine and ts_rank scales never need reconciling. The vector side is gated by
# `:min_sim` (a far cosine can't ride in on nearness alone); the keyword side
# surfaces exact-term matches the embedding misses. Temporal scoping is the
# `starts_at` window — no decay — so callers exclude past events by passing
# `time_min = now`.
_CANDIDATE_POOL = 40

_CALENDAR_HYBRID_SQL = text(
    """
    WITH q AS (SELECT websearch_to_tsquery('english', :raw_q) AS tsq),
    vec AS (
        SELECT d.id,
               row_number() OVER (ORDER BY d.embedding <=> CAST(:emb AS vector)) AS rnk
        FROM documents d
        WHERE d.provider = 'calendar'
          AND d.embedding IS NOT NULL
          AND (1.0 - (d.embedding <=> CAST(:emb AS vector))) >= :min_sim
          AND (:time_min IS NULL OR d.starts_at >= CAST(:time_min AS timestamptz))
          AND (:time_max IS NULL OR d.starts_at < CAST(:time_max AS timestamptz))
        ORDER BY d.embedding <=> CAST(:emb AS vector)
        LIMIT :pool
    ),
    kw AS (
        SELECT d.id,
               row_number() OVER (ORDER BY ts_rank_cd(d.tsv, q.tsq) DESC) AS rnk
        FROM documents d, q
        WHERE d.provider = 'calendar'
          AND q.tsq @@ d.tsv
          AND (:time_min IS NULL OR d.starts_at >= CAST(:time_min AS timestamptz))
          AND (:time_max IS NULL OR d.starts_at < CAST(:time_max AS timestamptz))
        ORDER BY ts_rank_cd(d.tsv, q.tsq) DESC
        LIMIT :pool
    ),
    fused AS (
        SELECT coalesce(vec.id, kw.id) AS id,
               coalesce(1.0 / (60 + vec.rnk), 0.0)
                 + coalesce(1.0 / (60 + kw.rnk), 0.0) AS score
        FROM vec FULL OUTER JOIN kw ON vec.id = kw.id
    )
    SELECT
      d.metadata->>'event_id' AS event_id,
      d.metadata->>'calendar_id' AS calendar_id,
      d.title AS summary,
      d.metadata->>'location' AS location,
      d.starts_at,
      d.url AS url,
      1.0 - (d.embedding <=> CAST(:emb AS vector)) AS similarity
    FROM fused JOIN documents d ON d.id = fused.id
    ORDER BY fused.score DESC, d.starts_at ASC
    LIMIT :k
    """
).bindparams(
    bindparam("emb", type_=String()),
    bindparam("raw_q", type_=String()),
    bindparam("time_min", type_=String()),
    bindparam("time_max", type_=String()),
)


def search_calendar_semantic(
    session: Session,
    *,
    embedding: list[float],
    raw_text: str,
    k: int = 8,
    min_similarity: float = 0.0,
    time_min: str | None = None,
    time_max: str | None = None,
) -> list[CalendarMatch]:
    """Cached calendar events by hybrid similarity + keyword (RRF over pgvector
    and Postgres FTS). The vector side is gated by `min_similarity`; keyword
    matches surface regardless. Scoped to `[time_min, time_max)` on the event's
    start — pass `time_min=now` to drop past events. Ties break to the sooner
    event."""
    emb_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    rows = session.execute(
        _CALENDAR_HYBRID_SQL,
        {
            "emb": emb_literal,
            "raw_q": raw_text or "",
            "k": k,
            "pool": _CANDIDATE_POOL,
            "min_sim": min_similarity,
            "time_min": time_min,
            "time_max": time_max,
        },
    ).all()
    return [
        CalendarMatch(
            event_id=r.event_id,
            calendar_id=r.calendar_id,
            summary=r.summary or "(untitled)",
            location=r.location,
            starts_at=r.starts_at,
            similarity=float(r.similarity) if r.similarity is not None else 0.0,
            url=r.url,
        )
        for r in rows
        if r.event_id
    ]
