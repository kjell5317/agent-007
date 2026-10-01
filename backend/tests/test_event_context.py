from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services import event_context


def test_event_deadline_is_before_start_unless_explicit(monkeypatch):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    monkeypatch.setattr(
        event_context.documents_store, "get_by_external_id",
        lambda *_a, **_k: SimpleNamespace(starts_at=start),
    )
    monkeypatch.setattr(
        event_context, "get_settings",
        lambda: SimpleNamespace(event_buffer_minutes=15),
    )
    fields = {
        "related_event_id": "xy", "related_event_calendar_id": "primary",
        "due_date": start + timedelta(hours=1),
    }
    derived, warning = event_context.apply_event_context(object(), fields)
    assert warning is None
    assert derived["due_date"] == start - timedelta(minutes=15)
    assert derived["related_event_due_derived"] is True

    explicit, warning = event_context.apply_event_context(object(), fields, explicit_due=True)
    assert explicit["due_date"] == fields["due_date"]
    assert "explicit due date" in warning
    assert explicit["related_event_due_derived"] is False


def test_event_context_is_conditional_without_link():
    fields, warning = event_context.apply_event_context(object(), {"title": "Send report"})
    assert fields == {"title": "Send report"}
    assert warning is None


def test_event_starting_too_soon_does_not_create_past_deadline(monkeypatch):
    start = datetime.now(timezone.utc) + timedelta(minutes=8)
    monkeypatch.setattr(
        event_context.documents_store, "get_by_external_id",
        lambda *_a, **_k: SimpleNamespace(starts_at=start),
    )
    monkeypatch.setattr(
        event_context, "get_settings",
        lambda: SimpleNamespace(event_buffer_minutes=15),
    )
    fields, warning = event_context.apply_event_context(
        object(), {"related_event_id": "xy", "related_event_calendar_id": "primary",
                   "due_date": start}
    )
    assert fields["due_date"] == start
    assert fields["related_event_due_derived"] is False
    assert "too soon" in warning
