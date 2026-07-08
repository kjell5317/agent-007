"""Cross-corpus suggest query for stage-1 search.

One UNION over tasks / raw_inputs / documents (notes are excluded — they aren't
navigable and only added scan cost), each row tagged with its type and ranked
by `ts_rank × recency decay`. Two modes: a match mode driven by a prefix
tsquery, and a recent mode (no free text) that just orders the filtered corpus
by recency.

Every branch matches against a stored, GIN-indexed `tsv` generated column
(`tasks.tsv`, `raw_inputs.tsv`, `documents.tsv`), so a query is index-backed FTS
rather than a per-row `to_tsvector` + `similarity()` scan. That drops trigram
typo-matching in exchange for the speed; the `:*` prefix on the last token still
covers as-you-type partial words.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, bindparam, text
from sqlalchemy.orm import Session

TASK = "task"
INPUT = "input"
DOCUMENT = "document"
ALL_CORPORA = frozenset({TASK, INPUT, DOCUMENT})


@dataclass
class SuggestHit:
    type: str
    id: str
    title: str
    snippet: str | None
    url: str | None
    task_id: str | None
    # Unified origin: an input's source (gmail/…) or a document's provider
    # (calendar/…). For tasks it's the source of their most recent input.
    source: str | None
    # Sender of the (last, for tasks) input; None for documents/manual.
    sender: str | None
    # Lifecycle status: task's derived status, input's status, 'event' for
    # calendar documents.
    status: str | None
    # Recency anchor shown as the date — the last input's time for tasks.
    ts: datetime | None
    score: float


# Per-corpus SQL fragments. `fts` is the stored, GIN-indexed tsvector column;
# `ts` the recency anchor. The projected column list is identical across
# branches so they UNION cleanly. (Searched text per corpus is defined by each
# table's generated `tsv` column: tasks = title+description+label, raw_inputs =
# subject+from+channel+content, documents = title+snippet+content.)
# A task's display fields come from its most-recent non-duplicate input (the
# same anchor `tasks.latest_status_for` uses): status, sender, source and the
# "last input" date. One LATERAL join yields all four.
_TASK_LAST_INPUT = (
    "tasks t LEFT JOIN LATERAL ("
    "SELECT ri.source, ri.source_metadata->>'from' AS sender, "
    "ri.received_at, ri.status "
    "FROM raw_inputs ri "
    "WHERE ri.task_id = t.id AND ri.status <> 'duplicate' "
    "ORDER BY ri.received_at DESC LIMIT 1"
    ") li ON true"
)

_BRANCHES: dict[str, dict[str, str]] = {
    TASK: {
        "from": _TASK_LAST_INPUT,
        "fts": "t.tsv",
        "ts": "coalesce(li.received_at, t.updated_at, t.created_at)",
        "select": (
            "'task' AS type, t.id::text AS id, t.title AS title, "
            "left(coalesce(t.description,''), 200) AS snippet, t.link AS url, "
            "t.id::text AS task_id, li.source AS source, li.sender AS sender, "
            "coalesce(li.status, 'open') AS status"
        ),
    },
    INPUT: {
        "from": "raw_inputs r",
        "fts": "r.tsv",
        "ts": "r.received_at",
        "select": (
            "'input' AS type, r.id::text AS id, "
            "coalesce(nullif(r.source_metadata->>'subject',''), "
            "left(coalesce(r.content,''), 80)) AS title, "
            "left(coalesce(r.content,''), 200) AS snippet, NULL::text AS url, "
            "r.task_id::text AS task_id, r.source AS source, "
            "r.source_metadata->>'from' AS sender, r.status AS status"
        ),
    },
    DOCUMENT: {
        "from": "documents d",
        "fts": "d.tsv",
        "ts": "coalesce(d.starts_at, d.updated_at, now())",
        "select": (
            "'document' AS type, d.id::text AS id, d.title AS title, "
            "coalesce(d.snippet, left(coalesce(d.content,''), 200)) AS snippet, d.url AS url, "
            "NULL::text AS task_id, d.provider AS source, "
            "NULL::text AS sender, 'event'::text AS status"
        ),
    },
}

# A task's derived status is its latest non-duplicate anchor's status; coalesce
# so an anchor-less task counts as open.
_TASK_STATUS_CLAUSE = "coalesce(li.status, 'open') = :status"


# Thread identity of an input — every input on a thread resolves to the same
# source link, so a thread collapses to one hit. Mirrors the inbox grouping:
# source-scoped thread id, else the row stands alone.
_INPUT_THREAD_KEY = (
    "CASE WHEN coalesce(r.source_metadata->>'thread_id', '') <> '' "
    "THEN r.source || ':' || (r.source_metadata->>'thread_id') "
    "ELSE 'input:' || r.id::text END"
)

_HIT_COLUMNS = "type, id, title, snippet, url, task_id, source, sender, status, ts, score"


def _branch_sql(corpus: str, *, match: bool, filters_sql: str) -> str:
    b = _BRANCHES[corpus]
    recency = f"exp(- extract(epoch from (now() - {b['ts']})) / (:half_life * 86400.0))"
    if match:
        tsq = "to_tsquery('english', :tsquery)"
        score = f"ts_rank_cd({b['fts']}, {tsq}) * {recency}"
        where = f"{b['fts']} @@ {tsq}"
    else:
        score = recency
        where = "TRUE"
    select = f"{b['select']}, {b['ts']} AS ts, {score} AS score"

    if corpus == INPUT:
        # Keep only the best-scoring (then newest) input per thread — DISTINCT ON
        # picks it before the outer query re-ranks everything by score.
        inner = (
            f"SELECT DISTINCT ON ({_INPUT_THREAD_KEY}) {select} "
            f"FROM {b['from']} WHERE {where}{filters_sql} "
            f"ORDER BY {_INPUT_THREAD_KEY}, score DESC, {b['ts']} DESC"
        )
        return f"SELECT {_HIT_COLUMNS} FROM ({inner}) input_by_thread"

    return f"SELECT {select} FROM {b['from']} WHERE {where}{filters_sql}"


def _filters_sql(
    corpus: str, *, source, label, status, before, after, exclude_linked_inputs: bool
) -> str:
    parts: list[str] = []
    ts = _BRANCHES[corpus]["ts"]
    # CAST(...) not `::timestamptz`: SQLAlchemy's text() bind-param regex skips
    # `:name` when it's followed by a colon (to leave `x::type` casts alone), so
    # `:before::timestamptz` would render `:before` unbound.
    parts.append(f"(:before IS NULL OR {ts} < CAST(:before AS timestamptz))")
    parts.append(f"(:after IS NULL OR {ts} >= CAST(:after AS timestamptz))")
    if corpus == TASK:
        if label is not None:
            # Case-insensitive: labels are capitalized in config (Uni, CSEE, …)
            # but users type `label:uni`.
            parts.append("lower(t.label) = lower(:label)")
        if status is not None:
            parts.append(_TASK_STATUS_CLAUSE)
    if corpus == INPUT:
        # One `source:` filter spans both origin corpora — matched against the
        # input's `source` column and the document's `provider` column.
        if source is not None:
            parts.append("r.source = :source")
        # Distinct on the task: an input folded into a task is represented by
        # that task's row, so drop it (many inputs → one task, shown once).
        # Only when the task branch is present to stand in for it — otherwise
        # (e.g. `source:gmail`, inputs-only) the input must still surface.
        if exclude_linked_inputs:
            parts.append("r.task_id IS NULL")
    if corpus == DOCUMENT:
        # kotx documents are always tied to a task, and that task already shows
        # (distinct) — so they never surface as their own hit.
        parts.append("d.provider <> 'kotx'")
        if source is not None:
            parts.append("d.provider = :source")
    return "".join(f" AND {p}" for p in parts)


def suggest(
    session: Session,
    *,
    tsquery: str,
    branches: frozenset[str],
    limit: int,
    half_life_days: float,
    source: str | None = None,
    label: str | None = None,
    status: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> list[SuggestHit]:
    match = bool(tsquery)
    active = [c for c in (TASK, INPUT, DOCUMENT) if c in branches]
    if not active:
        return []

    # Dedup inputs into their task only when the task branch is present to
    # represent them (see _filters_sql).
    exclude_linked_inputs = TASK in branches
    union = " UNION ALL ".join(
        _branch_sql(
            c,
            match=match,
            filters_sql=_filters_sql(
                c,
                source=source,
                label=label,
                status=status,
                before=before,
                after=after,
                exclude_linked_inputs=exclude_linked_inputs,
            ),
        )
        for c in active
    )
    # Type the free-standing `:before`/`:after` params: they appear as
    # `:x IS NULL` with no column to infer from, so Postgres rejects the bare
    # (possibly NULL) parameter without a declared type.
    sql = text(
        f"SELECT * FROM ({union}) hits ORDER BY score DESC, ts DESC NULLS LAST LIMIT :limit"
    ).bindparams(
        bindparam("before", type_=String()),
        bindparam("after", type_=String()),
    )

    rows = session.execute(
        sql,
        {
            "tsquery": tsquery,
            "half_life": half_life_days,
            "limit": limit,
            "source": source,
            "label": label,
            "status": status,
            "before": before,
            "after": after,
        },
    ).all()
    return [
        SuggestHit(
            type=r.type,
            id=r.id,
            title=r.title or "",
            snippet=r.snippet,
            url=r.url,
            task_id=r.task_id,
            source=r.source,
            sender=r.sender,
            status=r.status,
            ts=r.ts,
            score=float(r.score) if r.score is not None else 0.0,
        )
        for r in rows
    ]
