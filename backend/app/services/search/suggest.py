"""Stage-1 suggest orchestration: parse → query → cache.

`run_suggest` is the single entry point both the JSON endpoint and the SSE
stream call, so a cache hit serves either shape for free. The cache is a tiny
in-process TTL map keyed by the normalized query + limit — the same query
re-fires constantly (backspace, retype), and the TTL is short enough that
freshly created tasks show up within seconds.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import search as search_client
from app.db.models.raw_input import RawInput
from app.db.schemas.search import SearchHit
from app.services.search.filters import ALL_CORPORA, build_tsquery, corpus_restriction, parse_query
from app.services.source_url import source_url_for_raw_input

_CACHE_MAX = 512
_cache: dict[str, tuple[float, list[SearchHit]]] = {}


def run_suggest(
    session: Session,
    query: str,
    *,
    limit: int | None = None,
    types: frozenset[str] | None = None,
) -> list[SearchHit]:
    """Stage-1 suggestions. `types` restricts the corpora searched (the task
    composer asks for tasks + documents only); it intersects with any corpus
    restriction the filter tokens imply, so an empty intersection yields no hits."""
    settings = get_settings()
    limit = limit or settings.search_suggest_limit
    types_key = ",".join(sorted(types)) if types else "all"
    key = f"{query.strip().lower()}|{limit}|{types_key}"

    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    text, filters = parse_query(query)
    branches = corpus_restriction(filters) or ALL_CORPORA
    if types is not None:
        branches = frozenset(branches & types)

    if branches:
        hits = search_client.suggest(
            session,
            tsquery=build_tsquery(text),
            branches=branches,
            limit=limit,
            half_life_days=settings.search_recency_half_life_days,
            source=filters.source,
            label=filters.label,
            status=filters.status,
            before=filters.before,
            after=filters.after,
        )
        result = [SearchHit.build(h) for h in hits]
        result = _drop_documents_shadowed_by_tasks(result)
        _attach_input_source_urls(session, result)
    else:
        result = []

    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = (now + settings.search_suggest_cache_ttl_seconds, result)
    return result


def _drop_documents_shadowed_by_tasks(hits: list[SearchHit]) -> list[SearchHit]:
    """A document linked to a task that also matched (a kotx brief whose task is
    already in the results) is the same destination twice — keep the task, drop
    the document. A brief whose task didn't match still surfaces on its own."""
    task_ids = {h.id for h in hits if h.type == "task"}
    return [h for h in hits if not (h.type == "document" and h.task_id in task_ids)]


def _attach_input_source_urls(session: Session, hits: list[SearchHit]) -> None:
    """Give input hits a deep link to their source (gmail thread, Slack message,
    …) so a suggestion can jump to the original — the UNION query can't build
    those source-specific URLs, so resolve them here (few hits, so N+1 is fine)."""
    for hit in hits:
        if hit.type != "input" or hit.url:
            continue
        try:
            raw = session.get(RawInput, uuid.UUID(hit.id))
        except ValueError:
            continue
        if raw is not None:
            hit.url = source_url_for_raw_input(raw)
