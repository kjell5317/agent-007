"""Work type and effort for cached calendar events.

Rules decide what they can whenever an event is cached (`rule_attributes`);
the events they leave open are classified in batches by the cheap model
(`classify_pending_events`). The values live in the event's
`documents.metadata` under `work_type`, `effort` and `attr_source`
("rule", "label" or "model").
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import documents as documents_store
from app.db.clients import labels as labels_store
from app.db.models.document import Document
from app.services.attribute_classifier import BATCH_SIZE, BatchClassifier, numbered
from app.services.calendar.client import CalendarEvent
from app.services.calendar.events import is_free_event, is_managed_event
from app.services.location import is_online_location

log = logging.getLogger(__name__)

LabelDefaults = Mapping[str, tuple[str | None, str | None]]
LabelCatalog = tuple[LabelDefaults, Mapping[str, str]]

LOOKBACK = timedelta(days=1)

CLASSIFIER = BatchClassifier(
    tool_name="classify_events",
    description="Record the work type and effort of every listed calendar event.",
    items="events",
    subject="calendar event",
    fields=("work_type", "effort"),
    system_prompt=(
        "Classify every numbered event from the user's calendar by what attending "
        "it demands of the user. Call classify_events once with one entry per "
        "event, keyed by its number. Judge from the title, notes, label, location "
        "and duration."
    ),
    trace_name="classify-event-attributes",
)


def label_catalog(session: Session) -> LabelCatalog:
    return labels_store.defaults_by_name(session), labels_store.names_by_google_id(session)


def is_unrated(event: CalendarEvent) -> bool:
    return event.all_day or is_free_event(event) or is_managed_event(event)


def is_meeting(event: CalendarEvent) -> bool:
    if event.raw.get("hangoutLink") or event.raw.get("conferenceData"):
        return True
    # `is_online_location` treats an empty location as online.
    if (event.location or "").strip() and is_online_location(event.location):
        return True
    attendees = event.raw.get("attendees")
    # Rooms are listed as attendees too (`resource`), but booking one isn't company.
    return isinstance(attendees, list) and any(
        isinstance(attendee, dict) and not attendee.get("self") and not attendee.get("resource")
        for attendee in attendees
    )


def rule_attributes(
    event: CalendarEvent, label_defaults: LabelDefaults, label_names: Mapping[str, str]
) -> tuple[str | None, str | None, str | None]:
    """`(work_type, effort, source)`; all None when the rules leave it open."""
    if is_unrated(event):
        return None, None, None
    label = label_names.get(event.raw.get("eventLabelId") or "")
    default_work_type, default_effort = label_defaults.get(label or "", (None, None))
    if is_meeting(event):
        return "social", default_effort, "rule"
    if default_work_type or default_effort:
        return default_work_type, default_effort, "label"
    return None, None, None


def cached_attributes(
    event: CalendarEvent,
    previous: Mapping[str, Any] | None,
    *,
    content_changed: bool,
    label_defaults: LabelDefaults,
    label_names: Mapping[str, str],
) -> dict[str, str]:
    """The `work_type` / `effort` / `attr_source` metadata keys for a (re)cached
    event. A model classification survives only while the event's text is
    unchanged and no rule applies."""
    work_type, effort, source = rule_attributes(event, label_defaults, label_names)
    if (
        source is None
        and not is_unrated(event)
        and not content_changed
        and previous is not None
        and previous.get("attr_source") == "model"
    ):
        work_type, effort, source = previous.get("work_type"), previous.get("effort"), "model"
    values = {"work_type": work_type, "effort": effort, "attr_source": source}
    return {key: value for key, value in values.items() if value is not None}


def entry_text(event: Mapping[str, Any]) -> str:
    lines = [event["title"]]
    facts = [f"{key}: {event[key]}" for key in ("label", "location") if event.get(key)]
    if event.get("minutes"):
        facts.append(f"{event['minutes']} min")
    if facts:
        lines.append("   " + " · ".join(facts))
    notes = " ".join((event.get("notes") or "").split())
    if notes:
        lines.append(f"   notes: {notes}")
    return "\n".join(lines)


def batch_message(events: Sequence[Mapping[str, Any]]) -> str:
    return numbered([entry_text(event) for event in events])


def _notes(title: str, snippet: str | None) -> str:
    # The cached snippet is "<title> — <description>"; the title is listed already.
    return (snippet or "").removeprefix(title.strip()).removeprefix(" — ").strip()


def _snapshot(row: Document, label_names: Mapping[str, str]) -> dict[str, Any]:
    meta = row.meta or {}
    minutes = None
    if row.starts_at is not None and row.ends_at is not None:
        minutes = round((row.ends_at - row.starts_at).total_seconds() / 60)
    return {
        "title": row.title or "(untitled)",
        "notes": _notes(row.title or "", row.snippet),
        "label": label_names.get(meta.get("label_id") or ""),
        "location": meta.get("location"),
        "minutes": minutes,
    }


async def classify_pending_events(session: Session, *, until: datetime) -> int:
    """Classify the cached events starting between a day ago and `until` that
    the rules left open. Returns how many got values; a failed batch stays
    pending for the next run."""
    rows = documents_store.calendar_unclassified(
        session, since=datetime.now(timezone.utc) - LOOKBACK, until=until
    )
    if not rows:
        return 0
    settings = get_settings()
    label_names = labels_store.names_by_google_id(session)
    classified = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        entries = [entry_text(_snapshot(row, label_names)) for row in batch]
        try:
            suggestions = await CLASSIFIER.classify(entries, settings)
        except Exception:  # noqa: BLE001 — best effort; the batch stays pending
            log.exception("event classification failed · batch starting at %d", start)
            continue
        for number, row in enumerate(batch, start=1):
            fields = suggestions.get(number)
            if fields:
                row.meta = {**(row.meta or {}), **fields, "attr_source": "model"}
                classified += 1
        session.flush()
    return classified


def event_attributes(
    session: Session, refs: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    """`{(calendar_id, event_id): (work_type, effort)}` for the cached events among `refs`."""
    by_external_id = {f"{cal_id}:{event_id}": (cal_id, event_id) for cal_id, event_id in refs}
    metadata = documents_store.calendar_metadata(session, list(by_external_id))
    return {
        by_external_id[external_id]: (meta.get("work_type"), meta.get("effort"))
        for external_id, meta in metadata.items()
    }
