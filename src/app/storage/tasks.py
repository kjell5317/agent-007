from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import String, bindparam, select, text
from sqlalchemy.orm import Session

from app.models.task import Task
from app.schemas.task import TaskCreate


def create(
    session: Session,
    payload: TaskCreate,
    *,
    embedding: list[float] | None = None,
) -> Task:
    row = Task(
        title=payload.title,
        description=payload.description,
        estimated_minutes=payload.estimated_minutes,
        confidence=payload.confidence,
        location=payload.location,
        due_at=payload.due_at,
        source_links=list(payload.source_links or []),
        raw_input_id=payload.raw_input_id,
        embedding=embedding,
    )
    session.add(row)
    session.flush()
    return row


def get(session: Session, task_id: uuid.UUID) -> Task | None:
    return session.get(Task, task_id)


def list_(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[Task]:
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    return list(session.execute(stmt).scalars())


def update(
    session: Session,
    task_id: uuid.UUID,
    *,
    title: str | None = None,
    description: str | None = None,
    estimated_minutes: int | None = None,
    location: str | None = None,
    due_at: datetime | None = None,
    source_links: list[str] | None = None,
    status: str | None = None,
    embedding: list[float] | None = None,
) -> Task | None:
    row = session.get(Task, task_id)
    if row is None:
        return None
    if title is not None:
        row.title = title
    if description is not None:
        row.description = description
    if estimated_minutes is not None:
        row.estimated_minutes = estimated_minutes
    if location is not None:
        row.location = location
    if due_at is not None:
        row.due_at = due_at
    if source_links is not None:
        row.source_links = list(source_links)
    if status is not None:
        row.status = status
    if embedding is not None:
        row.embedding = embedding
    session.flush()
    return row


# Hybrid search ----------------------------------------------------------------
#
# Two retrieval passes — pgvector cosine distance and Postgres full-text rank —
# fused via Reciprocal Rank Fusion: score = Σ 1/(rrf_k + rank_i).
#
# RRF is rank-based, not score-based, so the two retrievers don't need to be
# on the same scale. The constant k=60 is the typical default and damps the
# contribution of low-ranked hits.
#
# For a personal-use prototype this runs without indexes (PG scans open tasks
# on the fly). At scale, add: a GIN index on a generated `to_tsvector` column,
# and an IVFFlat / HNSW index on `embedding`.

_RRF_K = 60
_CANDIDATE_POOL = 50

# `simple` text-search config keeps tokens verbatim (no stemming/stop-words),
# which is the safest default for multilingual content (de + en).
_FTS_CONFIG = "simple"

_HYBRID_SQL = text(
    f"""
    WITH
      vec AS (
        SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> CAST(:emb AS vector)) AS rnk
        FROM tasks
        WHERE status = 'open'
          AND embedding IS NOT NULL
          AND CAST(:emb AS vector) IS NOT NULL
        ORDER BY embedding <=> CAST(:emb AS vector)
        LIMIT :pool
      ),
      kw AS (
        SELECT id,
               ROW_NUMBER() OVER (
                 ORDER BY ts_rank_cd(
                   to_tsvector('{_FTS_CONFIG}', coalesce(title,'') || ' ' || coalesce(description,'')),
                   plainto_tsquery('{_FTS_CONFIG}', :q)
                 ) DESC
               ) AS rnk
        FROM tasks
        WHERE status = 'open'
          AND to_tsvector('{_FTS_CONFIG}', coalesce(title,'') || ' ' || coalesce(description,''))
              @@ plainto_tsquery('{_FTS_CONFIG}', :q)
        LIMIT :pool
      ),
      fused AS (
        SELECT id, SUM(1.0 / (:rrf_k + rnk)) AS score
        FROM (
          SELECT id, rnk FROM vec
          UNION ALL
          SELECT id, rnk FROM kw
        ) u
        GROUP BY id
      )
    SELECT id FROM fused
    ORDER BY score DESC
    LIMIT :k
    """
).bindparams(bindparam("emb", type_=String()), bindparam("q", type_=String()))


def search_similar(
    session: Session,
    *,
    query: str,
    embedding: list[float] | None,
    k: int = 10,
) -> list[Task]:
    """Hybrid candidate search over open tasks.

    Combines pgvector cosine similarity with Postgres full-text rank via RRF.
    Falls back gracefully when either signal is missing:
      - no embedding (no API key)  → keyword-only ranking
      - no FTS hits                → vector-only ranking
      - neither hits / empty table → returns the recent open tasks (least
        surprise: the agent still gets dedup candidates to consider)
    """
    q = (query or "").strip()
    has_query = bool(q)
    has_embedding = bool(embedding)

    if not has_query and not has_embedding:
        return list_(session, status="open", limit=k)

    # pgvector accepts the text form '[v1,v2,...]' via psycopg; the SQLAlchemy
    # Vector adapter isn't used here because we drop into raw text().
    emb_literal = (
        "[" + ",".join(repr(float(x)) for x in embedding) + "]" if embedding else None
    )

    rows = session.execute(
        _HYBRID_SQL,
        {
            "emb": emb_literal,
            "q": q,
            "pool": _CANDIDATE_POOL,
            "rrf_k": _RRF_K,
            "k": k,
        },
    ).all()

    if not rows:
        # Either no embedding *and* no FTS hits, or the open-task set is empty.
        # Recent-open is a useful fallback for the agent.
        return list_(session, status="open", limit=k)

    ordered_ids = [r.id for r in rows]
    fetched = session.execute(
        select(Task).where(Task.id.in_(ordered_ids))
    ).scalars().all()
    by_id = {t.id: t for t in fetched}
    return [by_id[i] for i in ordered_ids if i in by_id]
