"""Staged hybrid search (docs/search-plan.md).

Stage 1 lives here: `parse_query` splits filter tokens from free text,
`build_tsquery` turns the free text into a prefix-matching tsquery, and
`run_suggest` runs the cross-corpus UNION query behind a short TTL cache.
The DB-facing UNION SQL is in `app.db.clients.search`.
"""

from app.services.search.filters import Filters, build_tsquery, corpus_restriction, parse_query
from app.services.search.retrieve import retrieve
from app.services.search.suggest import run_suggest

__all__ = [
    "Filters",
    "build_tsquery",
    "corpus_restriction",
    "parse_query",
    "retrieve",
    "run_suggest",
]
