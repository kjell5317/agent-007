from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Float, String, bindparam, func, select, text
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
        source_raw_input_ids=[str(source_raw_input_id)] if source_raw_input_id else [],
    )
    session.add(row)
    session.flush()
    return row


def find_exact(session: Session, content: str) -> Note | None:
    return session.execute(
        select(Note).where(func.lower(func.trim(Note.content)) == content.strip().lower()).limit(1)
    ).scalar_one_or_none()


def find_exact_before(session: Session, content: str, note: Note) -> Note | None:
    return session.execute(
        select(Note).where(
            func.lower(func.trim(Note.content)) == content.strip().lower(),
            (Note.created_at < note.created_at)
            | ((Note.created_at == note.created_at) & (Note.id < note.id)),
        ).order_by(Note.created_at, Note.id).limit(1)
    ).scalar_one_or_none()


def add_source(session: Session, note_id: uuid.UUID, raw_input_id: uuid.UUID | None) -> None:
    note = session.get(Note, note_id)
    if note is not None:
        sources = list(note.source_raw_input_ids or [])
        if raw_input_id is not None and str(raw_input_id) not in sources:
            note.source_raw_input_ids = [*sources, str(raw_input_id)]
        note.updated_at = func.now()


def absorb_sources(session: Session, target_id: uuid.UUID, source: Note) -> None:
    target = session.get(Note, target_id)
    if target is None:
        return
    source_ids = list(source.source_raw_input_ids or [])
    if source.source_raw_input_id:
        source_ids.append(str(source.source_raw_input_id))
    target.source_raw_input_ids = list(dict.fromkeys([*(target.source_raw_input_ids or []), *source_ids]))
    if target.source_raw_input_id is None and source.source_raw_input_id is not None:
        target.source_raw_input_id = source.source_raw_input_id
    target.updated_at = func.now()


def mark_review(session: Session, note_id: uuid.UUID) -> None:
    note = session.get(Note, note_id)
    if note is not None:
        note.needs_review = True


@dataclass
class SimilarNote:
    id: uuid.UUID
    content: str
    similarity: float
    source_raw_input_id: uuid.UUID | None
    updated_at: datetime
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
        LEFT JOIN raw_inputs sr ON sr.id = n.source_raw_input_id
        WHERE n.embedding IS NOT NULL
          AND NOT n.needs_review
          AND (CAST(:source AS text) IS NULL OR coalesce(sr.source, 'chat') = :source)
          AND (CAST(:before_at AS timestamptz) IS NULL OR (n.created_at, n.id) <
               (CAST(:before_at AS timestamptz), CAST(:before_id AS uuid)))
          AND (1.0 - (n.embedding <=> CAST(:emb AS vector))) >= :min_sim
        ORDER BY n.embedding <=> CAST(:emb AS vector)
        LIMIT :pool
    ),
    kw AS (
        SELECT n.id, row_number() OVER (ORDER BY ts_rank_cd(n.tsv, q.tsq) DESC) AS rnk
        FROM notes n CROSS JOIN q
        LEFT JOIN raw_inputs sr ON sr.id = n.source_raw_input_id
        WHERE q.tsq @@ n.tsv
          AND NOT n.needs_review
          AND (CAST(:source AS text) IS NULL OR coalesce(sr.source, 'chat') = :source)
          AND (CAST(:before_at AS timestamptz) IS NULL OR (n.created_at, n.id) <
               (CAST(:before_at AS timestamptz), CAST(:before_id AS uuid)))
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
      n.id, n.content, n.source_raw_input_id, n.updated_at,
      r.source_metadata->>'from' AS source_from,
      r.source_metadata->>'subject' AS source_subject,
      1.0 - (n.embedding <=> CAST(:emb AS vector)) AS similarity
    FROM fused
    JOIN notes n ON n.id = fused.id
    LEFT JOIN raw_inputs r ON r.id = n.source_raw_input_id
    ORDER BY
      fused.rrf
        * exp(- EXTRACT(EPOCH FROM (now() - n.updated_at)) / (:half_life_days * 86400.0))
      DESC
    LIMIT :k
    """
).bindparams(
    bindparam("emb", type_=String()),
    bindparam("raw_q", type_=String()),
    bindparam("min_sim", type_=Float()),
)


def search_similar(
    session: Session,
    *,
    embedding: list[float],
    query: str,
    k: int = 5,
    min_similarity: float = 0.0,
    before: Note | None = None,
    source: str | None = None,
) -> list[SimilarNote]:
    """Top-k notes by hybrid similarity + keyword (RRF over pgvector and FTS),
    re-ranked with a mild recency decay. `min_similarity` gates the vector side
    only; keyword matches surface regardless."""
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
            "min_sim": min_similarity,
            "source": source,
            "before_at": before.created_at.isoformat() if before else None,
            "before_id": str(before.id) if before else None,
        },
    ).all()
    return [
        SimilarNote(
            id=r.id,
            content=r.content,
            similarity=float(r.similarity) if r.similarity is not None else 0.0,
            source_raw_input_id=r.source_raw_input_id,
            updated_at=r.updated_at,
            source_from=r.source_from,
            source_subject=r.source_subject,
        )
        for r in rows
    ]


def list_recent(session: Session, *, limit: int = 20) -> list[Note]:
    stmt = select(Note).order_by(Note.updated_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars())


@dataclass
class NoteListItem:
    id: uuid.UUID
    content: str
    source_raw_input_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    source: str | None
    source_from: str | None
    source_subject: str | None
    source_raw_input_ids: list[str] | None = None
    needs_review: bool = False
    history_count: int = 0
    content_update_count: int = 0
    last_content_update_at: datetime | None = None


# Enriches each note with its originating raw_input's source + sender/subject
# so the audit view can show where a memory came from. LEFT JOIN keeps notes
# whose source input was deleted (SET NULL) and chat-authored notes (no source).
_LIST_NOTES_TEMPLATE = """
    SELECT
      n.id, n.content, n.source_raw_input_id, n.source_raw_input_ids,
      n.needs_review, n.created_at,
      greatest(n.updated_at, coalesce(a.last_event_at, n.updated_at)) AS updated_at,
      coalesce(a.history_count, 0) AS history_count,
      coalesce(a.content_update_count, 0) AS content_update_count,
      a.last_content_update_at,
      r.source AS source,
      r.source_metadata->>'from' AS source_from,
      r.source_metadata->>'subject' AS source_subject
    FROM notes n
    LEFT JOIN (
      SELECT note_id,
        count(*) AS history_count,
        count(*) FILTER (WHERE action = 'content_updated') AS content_update_count,
        max(occurred_at) FILTER (WHERE action = 'content_updated') AS last_content_update_at,
        max(occurred_at) AS last_event_at
      FROM note_audit GROUP BY note_id
    ) a ON a.note_id = n.id
    LEFT JOIN raw_inputs r ON r.id = n.source_raw_input_id
    {where}
    ORDER BY updated_at DESC
    {limit}
"""


def _to_item(row) -> NoteListItem:
    return NoteListItem(
        id=row.id,
        content=row.content,
        source_raw_input_id=row.source_raw_input_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        source=row.source,
        source_from=row.source_from,
        source_subject=row.source_subject,
        source_raw_input_ids=row.source_raw_input_ids,
        needs_review=row.needs_review,
        history_count=row.history_count,
        content_update_count=row.content_update_count,
        last_content_update_at=row.last_content_update_at,
    )


@dataclass
class NoteAuditItem:
    action: str
    actor: str
    occurred_at: datetime
    old_content: str | None
    new_content: str | None
    source_raw_input_id: uuid.UUID | None
    needs_review: bool | None


def history(session: Session, note_id: uuid.UUID, *, limit: int = 100) -> list[NoteAuditItem]:
    rows = session.execute(text("""
        SELECT action, actor, occurred_at, old_content, new_content,
               source_raw_input_id, needs_review
        FROM note_audit WHERE note_id = :note_id
        ORDER BY occurred_at DESC, id DESC LIMIT :limit
    """), {"note_id": note_id, "limit": limit}).all()
    return [NoteAuditItem(**row._mapping) for row in rows]


def list_all(session: Session, *, limit: int = 500, source: str | None = None, query: str | None = None) -> list[NoteListItem]:
    clauses = []
    if source:
        clauses.append("coalesce(r.source, 'chat') = :source")
    if query:
        clauses.append("position(lower(:query) in lower(n.content)) > 0")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    stmt = text(_LIST_NOTES_TEMPLATE.format(where=where, limit="LIMIT :limit"))
    params = {"limit": limit}
    if source:
        params["source"] = source
    if query:
        params["query"] = query
    rows = session.execute(stmt, params).all()
    return [_to_item(r) for r in rows]


def get_item(session: Session, note_id: uuid.UUID) -> NoteListItem | None:
    stmt = text(_LIST_NOTES_TEMPLATE.format(where="WHERE n.id = :id", limit=""))
    row = session.execute(stmt, {"id": note_id}).first()
    return _to_item(row) if row is not None else None


def update(
    session: Session,
    note_id: uuid.UUID,
    *,
    content: str,
    embedding: list[float] | None,
) -> bool:
    row = session.get(Note, note_id)
    if row is None:
        return False
    row.content = content
    row.embedding = embedding
    row.needs_review = False
    row.updated_at = func.now()
    session.flush()
    return True


def delete(session: Session, note_id: uuid.UUID) -> bool:
    row = session.get(Note, note_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
