"""Chat retrieval: the up-front local pre-injection plus the per-source search
paths the chat agent's tools call on demand.

`retrieve` pre-loads the fast local corpus — tasks + notes only — into context
every turn, with zero external calls, so the common "what do I need to do about
X" answer needs no tool round-trip. Everything else is a dedicated per-source
path the agent invokes when the question calls for that source:

  * `search_messages` — the raw_inputs mirror (gmail/slack), hybrid.
  * `search_calendar`  — cached events (semantic) or a live window listing.
  * Drive / contacts / GitHub / Notion live in their own service modules.

All paths return `SearchHit`s so the runner renders one uniform record.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import documents as documents_store
from app.db.clients import notes as notes_store
from app.db.clients import search as search_client
from app.db.clients import tasks as tasks_store
from app.db.clients.search import INPUT, NOTE, TASK
from app.db.models.raw_input import RawInput
from app.db.schemas.search import SearchHit
from app.services.calendar.client import CalendarEvent
from app.services.calendar.events import list_events_between
from app.services.input.embedding import embed
from app.services.source_url import source_url_for_raw_input

# Pre-injection is the app's own task-tracking data: keyword-matched tasks and
# semantically-matched notes. Inputs, calendar, Drive and external sources are
# pulled on demand via their tools, never blended into the up-front context.
_LOCAL_CORPORA = frozenset({TASK, NOTE})


async def retrieve(session: Session, query: str) -> list[SearchHit]:
    """Up-front local pre-injection: tasks + notes only (keyword for tasks,
    hybrid keyword + semantic for notes). No external calls; a failing embed
    degrades to keyword-only."""
    query = (query or "").strip()
    if not query:
        return []
    settings = get_settings()
    embedding = await _embed_or_none(query)
    raw = search_client.hybrid_search(
        session,
        embedding=embedding,
        raw_text=query,
        k=settings.search_chat_local_limit,
        corpora=_LOCAL_CORPORA,
    )
    return [SearchHit.build(h) for h in raw]


async def search_messages(
    session: Session,
    query: str,
    *,
    source: str | None = None,
    before: str | None = None,
    after: str | None = None,
) -> list[SearchHit]:
    """Search ingested messages (the raw_inputs mirror) by hybrid similarity +
    keyword. `source` narrows to one origin (gmail/slack)."""
    query = (query or "").strip()
    if not query:
        return []
    settings = get_settings()
    embedding = await _embed_or_none(query)
    raw = search_client.hybrid_search(
        session,
        embedding=embedding,
        raw_text=query,
        k=settings.search_chat_messages_limit,
        corpora=frozenset({INPUT}),
        min_input_chars=settings.min_input_chars,
        source=source,
        before=before,
        after=after,
    )
    hits = [SearchHit.build(h) for h in raw]
    _attach_input_source_urls(session, hits)
    return hits


async def find_tasks(
    session: Session,
    *,
    query: str | None = None,
    status: str | None = None,
    label: str | None = None,
    due_after: str | None = None,
    due_before: str | None = None,
) -> list[SearchHit]:
    """The single task tool. With a `query`, keyword-search task text (ranked by
    relevance); without one, list tasks by status + due-date window ordered
    soonest-first — the reliable path for agenda questions, since task text
    rarely contains words like "today". `status`/`label` narrow both modes."""
    if (query or "").strip():
        return await search_tasks(session, query, label=label, status=status)
    return list_tasks(
        session,
        status=status or "open",
        due_after=due_after,
        due_before=due_before,
        label=label,
    )


async def search_tasks(
    session: Session, query: str, *, label: str | None = None, status: str | None = None
) -> list[SearchHit]:
    """Keyword search over the user's tasks (title/description/label). Tasks have
    no embedding, so this is keyword-only; `status` is a derived value, so it's
    applied as a post-filter on the resolved hits."""
    query = (query or "").strip()
    if not query:
        return []
    raw = search_client.hybrid_search(
        session,
        embedding=None,
        raw_text=query,
        k=get_settings().search_chat_local_limit,
        corpora=frozenset({TASK}),
        label=label,
    )
    hits = [SearchHit.build(h) for h in raw]
    if status:
        hits = [h for h in hits if (h.status or "") == status]
    return hits


async def search_notes(session: Session, query: str, *, k: int = 5) -> list[SearchHit]:
    """Search the agent's long-term memory (notes) by hybrid similarity + keyword."""
    query = (query or "").strip()
    if not query:
        return []
    embedding = await _embed_or_none(query)
    if embedding is None:
        return []
    hits = notes_store.search_similar(session, embedding=embedding, query=query, k=k)
    return [_note_hit(h) for h in hits]


async def search_calendar(
    session: Session,
    *,
    query: str | None = None,
    time_min: str | None = None,
    time_max: str | None = None,
) -> list[SearchHit]:
    """Find calendar events. With a `query`, hybrid-match cached events by
    meaning (upcoming only unless `time_min` overrides the floor); without one,
    list live events in the `[time_min, time_max)` window."""
    settings = get_settings()
    tz = ZoneInfo(settings.user_timezone)
    q = (query or "").strip()
    if q:
        embedding = await _embed_or_none(q)
        if embedding is not None:
            floor = time_min or datetime.now(tz).isoformat()
            matches = documents_store.search_calendar_semantic(
                session,
                embedding=embedding,
                raw_text=q,
                k=settings.calendar_semantic_match_limit,
                min_similarity=settings.calendar_semantic_min_similarity,
                time_min=floor,
                time_max=time_max,
            )
            return [_calendar_match_hit(m) for m in matches]
    return await _calendar_window(session, tz, time_min, time_max)


def list_tasks(
    session: Session,
    *,
    status: str = "open",
    due_after: str | None = None,
    due_before: str | None = None,
    label: str | None = None,
    limit: int = 25,
) -> list[SearchHit]:
    """Structured task listing for agenda questions ("today's todos", "overdue",
    "due this week") — no keywords needed, unlike hybrid `retrieve`. Filters by
    derived status + a window on each task's effective date (scheduled_date, else
    due_date), ordered soonest-first, and returns citeable task hits."""
    tz = ZoneInfo(get_settings().user_timezone)
    lo = _day_bound(due_after, tz)
    hi = _day_bound(due_before, tz)
    want_label = (label or "").strip().lower() or None

    out: list[SearchHit] = []
    for task, derived in tasks_store.list_(session, status=status, limit=200):
        if want_label and (task.label or "").lower() != want_label:
            continue
        eff = task.scheduled_date or task.due_date
        if lo is not None and (eff is None or eff < lo):
            continue
        if hi is not None and (eff is None or eff >= hi):
            continue
        out.append(
            SearchHit(
                type="task",
                id=str(task.id),
                title=task.title,
                snippet=(task.description or "")[:200] or None,
                url=task.link,
                task_id=str(task.id),
                source=task.label,
                sender=None,
                status=derived,
                ts=eff,
                score=1.0,
            )
        )
        if len(out) >= limit:
            break
    return out


async def _embed_or_none(query: str) -> list[float] | None:
    try:
        return await embed(query)
    except Exception:  # noqa: BLE001 — degrade to keyword-only on embed failure
        return None


def _day_bound(value: str | None, tz: ZoneInfo) -> datetime | None:
    """Parse a `YYYY-MM-DD` (or full ISO) boundary into a tz-aware datetime for
    comparison against stored timestamps."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


async def _calendar_window(
    session: Session, tz: ZoneInfo, time_min: str | None, time_max: str | None
) -> list[SearchHit]:
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "").strip()
    start = _day_bound(time_min, tz)
    end = _day_bound(time_max, tz)
    if not calendar_id or start is None or end is None or end <= start:
        return []
    events = await list_events_between(
        session, calendar_ids=[calendar_id], time_min=start, time_max=end
    )
    return [_calendar_event_hit(e) for e in events]


def _note_hit(h) -> SearchHit:
    meta = {k: v for k, v in {"from": h.source_from, "subject": h.source_subject}.items() if v}
    meta.update(_similarity_meta(h.similarity))
    return SearchHit(
        type="note",
        id=str(h.id),
        title=(h.content or "")[:80] or "(note)",
        snippet=(h.content or "")[:200] or None,
        url=None,
        source="note",
        status="note",
        ts=h.created_at,
        score=float(h.similarity),
        meta=meta or None,
    )


def _calendar_match_hit(m) -> SearchHit:
    meta = _calendar_meta(m.starts_at, m.location) or {}
    meta.update(_similarity_meta(m.similarity))
    return SearchHit(
        type="document",
        id=m.event_id,
        title=m.summary,
        snippet=m.location,
        url=m.url,
        source="calendar",
        status="event",
        ts=m.starts_at,
        score=float(m.similarity),
        meta=meta or None,
    )


def _calendar_event_hit(e: CalendarEvent) -> SearchHit:
    return SearchHit(
        type="document",
        id=e.id,
        title=e.summary or "(untitled)",
        snippet=e.location,
        url=e.html_link,
        source="calendar",
        status="event",
        ts=e.start,
        score=0.0,
        meta=_calendar_meta(e.start, e.location),
    )


def _similarity_meta(similarity: float | None) -> dict[str, float]:
    """Expose a genuine cosine score (semantic paths only) so the uniform record
    can show `sim=…`. RRF-ranked / keyword-only hits carry no meaningful cosine,
    so they get nothing rather than a misleading number."""
    return {"similarity": round(float(similarity), 2)} if similarity and similarity > 0 else {}


def _calendar_meta(starts_at: datetime | None, location: str | None) -> dict | None:
    meta: dict[str, str] = {}
    if starts_at is not None:
        meta["start"] = starts_at.isoformat()
    if location:
        meta["location"] = location
    return meta or None


def _attach_input_source_urls(session: Session, hits: list[SearchHit]) -> None:
    """Give input hits a deep link to their source (gmail thread, Slack message)
    so a citation can jump to the original."""
    for hit in hits:
        if hit.type != "input" or hit.url:
            continue
        try:
            raw = session.get(RawInput, uuid.UUID(hit.id))
        except ValueError:
            continue
        if raw is not None:
            hit.url = source_url_for_raw_input(raw)
