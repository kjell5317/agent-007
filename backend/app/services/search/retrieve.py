"""Stage-2 retrieval: embed the query once, then hybrid RRF across the local
corpora. The chat agent feeds these hits into the LLM context unconditionally
(the fast path — no tool round-trip to answer), and also calls it as the
`search` tool for follow-up drill-down.

Local only here; Drive federation lives in `app.services.search.drive` and is
merged by the chat runner.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.clients import search as search_client
from app.db.schemas.search import SearchHit
from app.services.input.embedding import embed
from app.services.source_url import source_url_for_raw_input


async def retrieve_local(session: Session, query: str, *, limit: int) -> list[SearchHit]:
    """Top hits across tasks / inputs / notes / documents by hybrid similarity +
    keyword. Embeddings unconfigured → keyword-only (embed returns None)."""
    query = (query or "").strip()
    if not query:
        return []
    embedding = await embed(query)
    raw = search_client.hybrid_search(session, embedding=embedding, raw_text=query, k=limit)
    hits = [SearchHit.build(h) for h in raw]
    _attach_input_source_urls(session, hits)
    return hits


def _attach_input_source_urls(session: Session, hits: list[SearchHit]) -> None:
    """Give input hits a deep link to their source (mirrors run_suggest) so a
    citation can jump to the original thread."""
    import uuid

    from app.db.models.raw_input import RawInput

    for hit in hits:
        if hit.type != "input" or hit.url:
            continue
        try:
            raw = session.get(RawInput, uuid.UUID(hit.id))
        except ValueError:
            continue
        if raw is not None:
            hit.url = source_url_for_raw_input(raw)
