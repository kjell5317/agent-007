"""Cross-corpus suggest query for stage-1 search.

One UNION over tasks / notes / raw_inputs / documents, each row tagged with its
type, ranked by `GREATEST(ts_rank, trigram similarity) × recency decay`. Two
modes: a match mode driven by a prefix tsquery, and a recent mode (no free
text) that just orders the filtered corpus by recency.

Trigram matching uses the function form `similarity() > threshold` rather than
the `%` operator: combined with the inline `to_tsvector` FTS (the owned tables
carry no FTS index) Postgres seq-scans these small tables either way, so the
operator would buy no index use while dragging in psycopg %-escaping. The
`documents` branch has real `tsv` + trigram GIN indexes for when a provider
sync populates it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, bindparam, text
from sqlalchemy.orm import Session

TASK = "task"
NOTE = "note"
INPUT = "input"
DOCUMENT = "document"
ALL_CORPORA = frozenset({TASK, NOTE, INPUT, DOCUMENT})

# Trigram floor for a typo/prefix title match. Permissive — typeahead wants
# recall, and FTS carries precision on the other side of the OR.
_TRG_THRESHOLD = 0.2


@dataclass
class SuggestHit:
    type: str
    id: str
    title: str
    snippet: str | None
    url: str | None
    task_id: str | None
    # Unified origin: an input's source (gmail/…) or a document's provider
    # (calendar/…). Null for tasks and notes.
    source: str | None
    ts: datetime | None
    score: float


# Per-corpus SQL fragments. `fts` is a tsvector expression, `trg` the text a
# trigram title match runs against, `ts` the recency anchor. The projected
# column list is identical across branches so they UNION cleanly.
_BRANCHES: dict[str, dict[str, str]] = {
    TASK: {
        "from": "tasks t",
        "fts": "to_tsvector('english', coalesce(t.title,'') || ' ' || coalesce(t.description,''))",
        "trg": "coalesce(t.title,'')",
        "ts": "coalesce(t.updated_at, t.created_at)",
        "select": (
            "'task' AS type, t.id::text AS id, t.title AS title, "
            "left(coalesce(t.description,''), 200) AS snippet, t.link AS url, "
            "t.id::text AS task_id, NULL::text AS source"
        ),
    },
    NOTE: {
        "from": "notes n",
        "fts": "to_tsvector('english', coalesce(n.content,''))",
        "trg": "coalesce(n.content,'')",
        "ts": "n.created_at",
        "select": (
            "'note' AS type, n.id::text AS id, "
            "left(coalesce(n.content,''), 100) AS title, "
            "left(coalesce(n.content,''), 240) AS snippet, NULL::text AS url, "
            "NULL::text AS task_id, NULL::text AS source"
        ),
    },
    INPUT: {
        "from": "raw_inputs r",
        "fts": (
            "to_tsvector('english', "
            "coalesce(r.source_metadata->>'subject','') || ' ' || coalesce(r.content,''))"
        ),
        "trg": (
            "coalesce(nullif(r.source_metadata->>'subject',''), left(coalesce(r.content,''), 80))"
        ),
        "ts": "r.received_at",
        "select": (
            "'input' AS type, r.id::text AS id, "
            "coalesce(nullif(r.source_metadata->>'subject',''), "
            "left(coalesce(r.content,''), 80)) AS title, "
            "left(coalesce(r.content,''), 200) AS snippet, NULL::text AS url, "
            "r.task_id::text AS task_id, r.source AS source"
        ),
    },
    DOCUMENT: {
        "from": "documents d",
        "fts": "d.tsv",
        "trg": "coalesce(d.title,'')",
        "ts": "coalesce(d.updated_at, d.starts_at, now())",
        "select": (
            "'document' AS type, d.id::text AS id, d.title AS title, "
            "coalesce(d.snippet, left(coalesce(d.content,''), 200)) AS snippet, d.url AS url, "
            "NULL::text AS task_id, d.provider AS source"
        ),
    },
}

# A task's derived status is its latest non-duplicate anchor's status (matches
# tasks.latest_status_for); coalesce so an anchor-less task counts as open.
_TASK_STATUS_CLAUSE = (
    ":status = coalesce((SELECT ri.status FROM raw_inputs ri "
    "WHERE ri.task_id = t.id AND ri.status <> 'duplicate' "
    "ORDER BY ri.received_at DESC LIMIT 1), 'open')"
)


def _branch_sql(corpus: str, *, match: bool, filters_sql: str) -> str:
    b = _BRANCHES[corpus]
    tsq = "to_tsquery('english', :tsquery)"
    recency = f"exp(- extract(epoch from (now() - {b['ts']})) / (:half_life * 86400.0))"
    if match:
        score = (
            f"greatest(ts_rank_cd({b['fts']}, {tsq}), similarity({b['trg']}, :raw)) * {recency}"
        )
        where = f"({b['fts']} @@ {tsq} OR similarity({b['trg']}, :raw) > :trg_threshold)"
    else:
        score = recency
        where = "TRUE"
    return (
        f"SELECT {b['select']}, {b['ts']} AS ts, {score} AS score "
        f"FROM {b['from']} "
        f"WHERE {where}{filters_sql}"
    )


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
            parts.append("t.label = :label")
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
    if corpus == DOCUMENT and source is not None:
        parts.append("d.provider = :source")
    return "".join(f" AND {p}" for p in parts)


def suggest(
    session: Session,
    *,
    tsquery: str,
    raw_text: str,
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
    active = [c for c in (TASK, NOTE, INPUT, DOCUMENT) if c in branches]
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
    # Type the free-standing params: `:before`/`:after` appear as `:x IS NULL`
    # with no column to infer from, and `:raw` feeds similarity(). Without a
    # declared type Postgres rejects the bare (possibly NULL) parameter. `:raw`
    # is only in the SQL in match mode, so declare it only then.
    binds = [bindparam("before", type_=String()), bindparam("after", type_=String())]
    if match:
        binds.append(bindparam("raw", type_=String()))
    sql = text(
        f"SELECT * FROM ({union}) hits ORDER BY score DESC, ts DESC NULLS LAST LIMIT :limit"
    ).bindparams(*binds)

    rows = session.execute(
        sql,
        {
            "tsquery": tsquery,
            "raw": raw_text,
            "trg_threshold": _TRG_THRESHOLD,
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
            ts=r.ts,
            score=float(r.score) if r.score is not None else 0.0,
        )
        for r in rows
    ]
