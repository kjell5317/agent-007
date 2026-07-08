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
        # `is:processing` / `is:event` filter inputs by their raw status
        # (the corpus router only sends non-task statuses here).
        if status is not None:
            parts.append("r.status = :status")
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


# --- Stage 2: cross-corpus hybrid retrieval (RRF) -----------------------------
#
# Unlike stage-1 suggest (a prefix-tsquery UNION for typeahead), stage-2 fuses a
# pgvector nearest-neighbour ranking with a Postgres FTS ranking via Reciprocal
# Rank Fusion — the same rank-based fusion the precedent/notes/calendar lookups
# use, generalized across all four corpora. Each corpus contributes its own
# index-backed top-`pool` (vector or FTS), the two sides are globally re-ranked,
# then fused; a second pass resolves display fields.
#
# What is embedded per corpus (the vector side; same Gemini space):
#   • raw_inputs — "from: <sender>[ in <channel>]" + subject + body[:1500]
#     (app.services.input.embedding.candidate_query_text), embedded at ingest.
#   • notes      — the note text (app.db.clients.notes / notes_lookup.save_notes).
#   • documents  — provider-specific text; here documents means kotx briefs
#     (TASK.md/REVIEW.md/PR). Calendar events live in documents too but are
#     EXCLUDED here — they're served by the dedicated semantic calendar search
#     (app.db.clients.documents.search_calendar_semantic) so events appear once.
#   • tasks      — NOT embedded (no embedding column). Tasks ride the keyword
#     side only; semantically-close tasks still surface via their linked inputs.
# The keyword side (FTS) matches each table's generated `tsv`: tasks =
# title+description+label, raw_inputs = subject+from+channel+content, notes =
# content, documents = title+snippet+content.
NOTE = "note"

# Per-corpus id column, used both for the FTS/vector pools and the display fetch.
_ID_COL = {TASK: "t.id", INPUT: "r.id", NOTE: "n.id", DOCUMENT: "d.id"}

# Display branch for notes (not part of the stage-1 UNION — notes aren't
# navigable there). Task link comes from the source input when it has one.
_NOTE_DISPLAY = {
    "from": "notes n LEFT JOIN raw_inputs nr ON nr.id = n.source_raw_input_id",
    "ts": "n.created_at",
    "select": (
        "'note' AS type, n.id::text AS id, "
        "left(coalesce(n.content,''), 80) AS title, "
        "left(coalesce(n.content,''), 240) AS snippet, NULL::text AS url, "
        "nr.task_id::text AS task_id, 'note'::text AS source, "
        "nr.source_metadata->>'from' AS sender, 'note'::text AS status"
    ),
}

# Stage-2 document display (own branch, not the stage-1 one). Calendar is served
# by the dedicated semantic search, so this covers kotx (and future providers).
# kotx docs are content-for-context but not distinct: link them to their task
# (join on kotx_task_id) so the citation opens the task, mirroring how an input
# hit resolves to its task.
_DOC_DISPLAY = {
    "from": (
        "documents d LEFT JOIN tasks kt ON kt.kotx_task_id = "
        "CASE WHEN d.provider = 'kotx' AND d.external_id ~ '^[0-9]+$' "
        "THEN d.external_id::int END"
    ),
    "ts": "coalesce(d.starts_at, d.updated_at, now())",
    "select": (
        "'document' AS type, d.id::text AS id, d.title AS title, "
        "coalesce(d.snippet, left(coalesce(d.content,''), 200)) AS snippet, "
        "coalesce(kt.link, d.url) AS url, kt.id::text AS task_id, "
        "d.provider AS source, NULL::text AS sender, NULL::text AS status"
    ),
}

_RETRIEVE_POOL = 40

# Optional metadata filters, threaded per corpus. Each is a pass-through when its
# bind is NULL, so the SQL stays a single constant. `source` maps to an input's
# source / a document's provider; `label`/`status` are task/input concepts;
# `before`/`after` bound each corpus's recency anchor.
_F_INPUT = (
    " AND char_length(coalesce(r.content,'')) >= :min_input_chars"
    " AND (:f_source IS NULL OR r.source = :f_source)"
    " AND (:f_status IS NULL OR r.status = :f_status)"
    " AND (:f_before IS NULL OR r.received_at < CAST(:f_before AS timestamptz))"
    " AND (:f_after IS NULL OR r.received_at >= CAST(:f_after AS timestamptz))"
)
_F_TASK = (
    " AND (:f_label IS NULL OR lower(t.label) = lower(:f_label))"
    " AND (:f_before IS NULL OR t.updated_at < CAST(:f_before AS timestamptz))"
    " AND (:f_after IS NULL OR t.updated_at >= CAST(:f_after AS timestamptz))"
)
_F_NOTE = (
    " AND (:f_before IS NULL OR n.created_at < CAST(:f_before AS timestamptz))"
    " AND (:f_after IS NULL OR n.created_at >= CAST(:f_after AS timestamptz))"
)
_F_DOC = (
    " AND (:f_source IS NULL OR d.provider = :f_source)"
    " AND (:f_before IS NULL OR d.starts_at < CAST(:f_before AS timestamptz))"
    " AND (:f_after IS NULL OR d.starts_at >= CAST(:f_after AS timestamptz))"
)

_HYBRID_SQL = text(
    f"""
    WITH q AS (SELECT websearch_to_tsquery('english', :raw_q) AS tsq),
    vec AS (
        SELECT id, type, row_number() OVER (ORDER BY dist) AS rnk
        FROM (
            (SELECT r.id::text AS id, 'input' AS type,
                    r.embedding <=> CAST(:emb AS vector) AS dist
               FROM raw_inputs r
              WHERE :emb IS NOT NULL AND r.embedding IS NOT NULL
                AND r.processed_at IS NOT NULL AND r.status <> 'duplicate'{_F_INPUT}
              ORDER BY r.embedding <=> CAST(:emb AS vector) LIMIT :pool)
            UNION ALL
            (SELECT n.id::text, 'note', n.embedding <=> CAST(:emb AS vector)
               FROM notes n
              WHERE :emb IS NOT NULL AND n.embedding IS NOT NULL{_F_NOTE}
              ORDER BY n.embedding <=> CAST(:emb AS vector) LIMIT :pool)
            UNION ALL
            (SELECT d.id::text, 'document', d.embedding <=> CAST(:emb AS vector)
               FROM documents d
              WHERE :emb IS NOT NULL AND d.embedding IS NOT NULL AND d.provider <> 'calendar'{_F_DOC}
              ORDER BY d.embedding <=> CAST(:emb AS vector) LIMIT :pool)
        ) v
        ORDER BY dist LIMIT :pool
    ),
    kw AS (
        SELECT id, type, row_number() OVER (ORDER BY rank DESC) AS rnk
        FROM (
            (SELECT t.id::text AS id, 'task' AS type, ts_rank_cd(t.tsv, q.tsq) AS rank
               FROM tasks t, q WHERE q.tsq @@ t.tsv{_F_TASK}
              ORDER BY ts_rank_cd(t.tsv, q.tsq) DESC LIMIT :pool)
            UNION ALL
            (SELECT r.id::text, 'input', ts_rank_cd(r.tsv, q.tsq)
               FROM raw_inputs r, q
              WHERE q.tsq @@ r.tsv AND r.processed_at IS NOT NULL
                AND r.status <> 'duplicate'{_F_INPUT}
              ORDER BY ts_rank_cd(r.tsv, q.tsq) DESC LIMIT :pool)
            UNION ALL
            (SELECT n.id::text, 'note', ts_rank_cd(n.tsv, q.tsq)
               FROM notes n, q WHERE q.tsq @@ n.tsv{_F_NOTE}
              ORDER BY ts_rank_cd(n.tsv, q.tsq) DESC LIMIT :pool)
            UNION ALL
            (SELECT d.id::text, 'document', ts_rank_cd(d.tsv, q.tsq)
               FROM documents d, q WHERE q.tsq @@ d.tsv AND d.provider <> 'calendar'{_F_DOC}
              ORDER BY ts_rank_cd(d.tsv, q.tsq) DESC LIMIT :pool)
        ) k
        ORDER BY rank DESC LIMIT :pool
    ),
    fused AS (
        SELECT coalesce(vec.id, kw.id) AS id, coalesce(vec.type, kw.type) AS type,
               coalesce(1.0 / (60 + vec.rnk), 0.0)
                 + coalesce(1.0 / (60 + kw.rnk), 0.0) AS rrf
        FROM vec FULL OUTER JOIN kw ON vec.id = kw.id AND vec.type = kw.type
    )
    SELECT type, id, rrf FROM fused ORDER BY rrf DESC LIMIT :k
    """
).bindparams(
    bindparam("emb", type_=String()),
    bindparam("raw_q", type_=String()),
    bindparam("f_source", type_=String()),
    bindparam("f_label", type_=String()),
    bindparam("f_status", type_=String()),
    bindparam("f_before", type_=String()),
    bindparam("f_after", type_=String()),
)


def _display_branch(corpus: str) -> dict[str, str]:
    if corpus == NOTE:
        return _NOTE_DISPLAY
    if corpus == DOCUMENT:
        return _DOC_DISPLAY
    return _BRANCHES[corpus]


def _load_display(session: Session, corpus: str, ids: list[str]) -> list[SuggestHit]:
    """Resolve display fields for the winning ids of one corpus, reusing the
    stage-1 select fragments so a hit reads identically to a suggest row."""
    b = _display_branch(corpus)
    sql = text(
        f"SELECT type, id, title, snippet, url, task_id, source, sender, status, ts FROM ("
        f"SELECT {b['select']}, {b['ts']} AS ts FROM {b['from']} "
        f"WHERE {_ID_COL[corpus]}::text = ANY(:ids)"
        f") x"
    )
    rows = session.execute(sql, {"ids": ids}).all()
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
            score=0.0,
        )
        for r in rows
    ]


def hybrid_search(
    session: Session,
    *,
    embedding: list[float] | None,
    raw_text: str,
    k: int,
    min_input_chars: int = 0,
    source: str | None = None,
    label: str | None = None,
    status: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> list[SuggestHit]:
    """Top-k hits across tasks / inputs / notes / documents by hybrid similarity
    + keyword (RRF over pgvector and Postgres FTS). `embedding=None` (embeddings
    unconfigured) degrades to keyword-only. Optional metadata filters
    (`source`/`label`/`status`/`before`/`after`) narrow the pool like the
    stage-1 filter tokens; inputs under `min_input_chars` are excluded. Hits
    carry the RRF score and come back in fused-rank order."""
    emb_literal = (
        "[" + ",".join(repr(float(x)) for x in embedding) + "]" if embedding else None
    )
    ranked = session.execute(
        _HYBRID_SQL,
        {
            "emb": emb_literal,
            "raw_q": raw_text or "",
            "pool": _RETRIEVE_POOL,
            "k": k,
            "min_input_chars": min_input_chars,
            "f_source": source,
            "f_label": label,
            "f_status": status,
            "f_before": before,
            "f_after": after,
        },
    ).all()
    if not ranked:
        return []

    order = {(r.type, r.id): i for i, r in enumerate(ranked)}
    rrf = {(r.type, r.id): float(r.rrf) for r in ranked}
    ids_by_type: dict[str, list[str]] = {}
    for r in ranked:
        ids_by_type.setdefault(r.type, []).append(r.id)

    hits: list[SuggestHit] = []
    for corpus, ids in ids_by_type.items():
        for hit in _load_display(session, corpus, ids):
            hit.score = rrf[(corpus, hit.id)]
            hits.append(hit)
    hits.sort(key=lambda h: order[(h.type, h.id)])
    return hits
