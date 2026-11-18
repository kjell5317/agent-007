"""Staged hybrid search.

Stage 1: `parse_query` splits filter tokens from free text, `build_tsquery`
turns the free text into a prefix-matching tsquery, and `run_suggest` runs the
cross-corpus UNION query behind a short TTL cache. Stage 2: `run_search` is
the hybrid (semantic + keyword + trigram) search behind Enter. The DB-facing
SQL is in `app.db.clients.search`.
"""

from app.services.search.search import run_search
from app.services.search.suggest import run_suggest

__all__ = ["run_search", "run_suggest"]
