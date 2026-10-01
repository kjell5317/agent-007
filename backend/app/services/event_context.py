"""Validate a task's source event and keep its completion deadline before it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

from app.config import get_settings
from app.db.clients import documents as documents_store

_EVENT_CUE = re.compile(
    r"\b(?:prepar\w*|vorbereit\w*|attend\w*|teilnehm\w*|meeting|event|"
    r"veranstalt\w*|conference|konferenz|workshop|webinar|appointment|termin|"
    r"presentation|präsentation|talk)\b",
    re.IGNORECASE,
)


def mentions_event(text: str) -> bool:
    return bool(_EVENT_CUE.search(text or ""))


def apply_event_context(session, fields: dict, *, explicit_due: bool = False) -> tuple[dict, str | None]:
    fields = dict(fields)
    event_id = str(fields.get("related_event_id") or "").strip()
    calendar_id = str(fields.get("related_event_calendar_id") or "").strip()
    if not event_id or not calendar_id:
        fields.pop("related_event_id", None)
        fields.pop("related_event_calendar_id", None)
        fields.pop("related_event_due_derived", None)
        return fields, None

    document = documents_store.get_by_external_id(
        session, provider="calendar", external_id=f"{calendar_id}:{event_id}"
    )
    start = getattr(document, "starts_at", None)
    now = datetime.now(timezone.utc)
    if start is None or start <= now:
        fields.pop("related_event_id", None)
        fields.pop("related_event_calendar_id", None)
        fields.pop("related_event_due_derived", None)
        return fields, "Related event was unavailable or had already started."

    buffer = timedelta(minutes=max(5, get_settings().event_buffer_minutes))
    event_deadline = start - buffer
    if event_deadline <= now:
        fields["related_event_due_derived"] = False
        return fields, "The related event starts too soon for a buffered preparation deadline."
    due = fields.get("due_date")
    if isinstance(due, str):
        due = datetime.fromisoformat(due.replace("Z", "+00:00"))
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if explicit_due:
        fields["related_event_due_derived"] = False
        if due is not None and due >= start:
            return fields, "The explicit due date is at or after the related event starts."
        return fields, None
    if due is None or due > event_deadline:
        fields["due_date"] = event_deadline
        fields["related_event_due_derived"] = True
    else:
        fields["related_event_due_derived"] = bool(fields.get("related_event_due_derived"))
    return fields, None
