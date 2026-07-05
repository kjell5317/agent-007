from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import String, bindparam, func, select, text, update
from sqlalchemy.orm import Session

from app.config.settings import get_settings
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


# Group key mirrors the frontend's `inputGroupKey`: a shared task wins, so every
# follow-up / duplicate of a task (incl. cross-thread, embedding-matched ones)
# folds in with its anchor; then a thread for pre-task inputs; else the row
# stands alone. Keeping the two in sync is what guarantees the rows we return
# for N groups are exactly N whole groups on the client.
_GROUPED_INPUT_IDS_SQL = text(
    """
    WITH keyed AS (
        SELECT
            id,
            received_at,
            CASE
                WHEN task_id IS NOT NULL
                    THEN 'task:' || task_id::text
                -- github:* thread keys are a cross-source namespace (gmail +
                -- kotx share them), so the source prefix is dropped.
                WHEN source_metadata->>'thread_id' LIKE 'github:%'
                    THEN 'thread:' || (source_metadata->>'thread_id')
                WHEN COALESCE(source_metadata->>'thread_id', '') <> ''
                    THEN source || ':thread:' || (source_metadata->>'thread_id')
                ELSE 'input:' || id::text
            END AS group_key
        FROM raw_inputs
        WHERE (CAST(:status AS text) IS NULL OR status = :status)
          AND (CAST(:source AS text) IS NULL OR source = :source)
    ),
    top_groups AS (
        SELECT group_key, MAX(received_at) AS group_sort
        FROM keyed
        GROUP BY group_key
        ORDER BY group_sort DESC
        LIMIT :limit
    )
    SELECT k.id
    FROM keyed k
    JOIN top_groups g ON k.group_key = g.group_key
    ORDER BY g.group_sort DESC, k.received_at DESC
    """
)


def list_grouped(
    session: Session,
    *,
    status: str | None = None,
    source: str | None = None,
    limit: int = 100,
) -> list[RawInput]:
    """Return raw inputs for the `limit` most-recent groups (thread / task /
    standalone), with *every* member of each group included.

    `limit` counts groups, not rows — so the inbox can render whole threads
    without one being split across a pagination boundary. Rows come ordered
    newest-group-first, newest-member-first within a group.
    """
    ordered_ids = [
        row.id
        for row in session.execute(
            _GROUPED_INPUT_IDS_SQL,
            {"status": status, "source": source, "limit": limit},
        )
    ]
    if not ordered_ids:
        return []
    objs = (
        session.execute(select(RawInput).where(RawInput.id.in_(ordered_ids)))
        .scalars()
        .all()
    )
    by_id = {o.id: o for o in objs}
    return [by_id[i] for i in ordered_ids if i in by_id]


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


def processing_state(
    session: Session, raw_input_id: uuid.UUID
) -> tuple[datetime | None, str] | None:
    """Fresh `(processed_at, status)` read that bypasses the identity map.

    A caller holding the per-kotx-id lock uses this to detect a redelivery
    that another session finalized while it waited — a plain `session.get`
    would hand back the stale in-transaction copy (still unprocessed).
    Returns None if the row is gone."""
    row = session.execute(
        select(RawInput.processed_at, RawInput.status).where(
            RawInput.id == raw_input_id
        )
    ).first()
    return (row.processed_at, row.status) if row is not None else None


def link_unassigned_by_thread(
    session: Session,
    *,
    source: str,
    thread_id: str,
    task_id: uuid.UUID,
) -> int:
    """Attach unlinked inputs on a source/thread to a task without changing status."""
    stmt = (
        update(RawInput)
        .where(
            RawInput.task_id.is_(None),
            RawInput.source == source,
            text("source_metadata->>'thread_id' = :thread_id"),
        )
        .values(task_id=task_id)
        .execution_options(synchronize_session=False)
    )
    result = session.execute(stmt, {"thread_id": thread_id})
    session.flush()
    return int(result.rowcount or 0)


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


def list_for_task(session: Session, task_id: uuid.UUID) -> list[RawInput]:
    """Return every raw input linked to a task, newest first.

    This mirrors the inbox grouping rule where a shared task_id wins, so
    duplicates and agent follow-ups are visible with the task they reference.
    """
    stmt = (
        select(RawInput)
        .where(RawInput.task_id == task_id)
        .order_by(RawInput.received_at.desc())
    )
    return list(session.execute(stmt).scalars())


def find_by_thread(
    session: Session,
    source: str | None,
    thread_id: str,
    *,
    metadata_filters: dict[str, str] | None = None,
) -> RawInput | None:
    """Return the most-recent raw_input on a scoped thread that linked to a
    task. `source=None` searches across sources — used for canonical thread
    namespaces (`github:owner/repo#N`) shared by gmail and kotx."""
    metadata_filters = metadata_filters or {}
    metadata_clauses = [
        RawInput.source_metadata[key].as_string() == value
        for key, value in metadata_filters.items()
    ]
    if source is not None:
        metadata_clauses.append(RawInput.source == source)
    stmt = (
        select(RawInput)
        .where(
            RawInput.task_id.is_not(None),
            text("source_metadata->>'thread_id' = :thread_id"),
            *metadata_clauses,
        )
        .order_by(RawInput.received_at.desc())
        .limit(1)
        .params(thread_id=thread_id)
    )
    return session.execute(stmt).scalar_one_or_none()


def find_kotx_by_pr(session: Session, repo: str, pr_number: int) -> RawInput | None:
    """Most-recent task-linked kotx input referencing this repo's PR. Ties a
    run anchored on the PR (a follow-up or resolve-conflict run) to the task
    whose issue-anchored run opened that PR — those transitions carry
    `pr_number` once the PR exists."""
    stmt = (
        select(RawInput)
        .where(
            RawInput.task_id.is_not(None),
            RawInput.source == "kotx",
            text("source_metadata->>'repo' = :repo"),
            text("source_metadata->>'pr_number' = :pr_number"),
        )
        .order_by(RawInput.received_at.desc())
        .limit(1)
        .params(repo=repo, pr_number=str(pr_number))
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
    decayed_similarity: float
    agent_trace: dict | None
    subject: str | None
    sender: str | None
    content_snippet: str | None
    received_at: datetime
    label: str | None = None


_SIMILAR_INPUTS_SQL = text(
    """
    SELECT
      raw_inputs.id, raw_inputs.source, raw_inputs.status, raw_inputs.task_id,
      tasks.label, raw_inputs.received_at, raw_inputs.agent_trace,
      raw_inputs.source_metadata,
      LEFT(raw_inputs.content, 1000) AS content_snippet,
      1.0 - (raw_inputs.embedding <=> CAST(:emb AS vector)) AS similarity,
      (1.0 - (raw_inputs.embedding <=> CAST(:emb AS vector)))
        * exp(- EXTRACT(EPOCH FROM (now() - raw_inputs.received_at))
              / (:half_life_days * 86400.0)) AS decayed_similarity
    FROM raw_inputs
    LEFT JOIN tasks ON tasks.id = raw_inputs.task_id
    WHERE raw_inputs.embedding IS NOT NULL
      AND raw_inputs.processed_at IS NOT NULL
      AND raw_inputs.status = ANY(:statuses)
      AND raw_inputs.id <> :exclude_id
    ORDER BY decayed_similarity DESC
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
    """Top-k past raw inputs by recency-decayed cosine similarity, filtered to
    the given statuses. `similarity` stays the raw cosine score;
    `decayed_similarity` drives ranking and the orchestrator's auto-decide."""
    emb_literal = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
    rows = session.execute(
        _SIMILAR_INPUTS_SQL,
        {
            "emb": emb_literal,
            "exclude_id": exclude_id,
            "statuses": statuses,
            "k": k,
            "half_life_days": get_settings().input_similarity_half_life_days,
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
                label=r.label,
                similarity=float(r.similarity) if r.similarity is not None else 0.0,
                decayed_similarity=(
                    float(r.decayed_similarity)
                    if r.decayed_similarity is not None
                    else 0.0
                ),
                agent_trace=r.agent_trace,
                subject=meta.get("subject"),
                sender=meta.get("from"),
                content_snippet=r.content_snippet,
                received_at=r.received_at,
            )
        )
    return out
