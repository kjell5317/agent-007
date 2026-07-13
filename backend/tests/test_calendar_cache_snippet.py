from datetime import datetime, timezone

from app.services.calendar.cache import _content, _snippet
from app.services.calendar.client import CalendarEvent


def _event(*, summary="Standup", description=None, location=None) -> CalendarEvent:
    now = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
    return CalendarEvent(
        id="e1",
        calendar_id="primary",
        summary=summary,
        description=description,
        start=now,
        end=now,
        all_day=False,
        location=location,
        html_link="https://cal/e1",
        private_properties={},
        raw={},
    )


def test_snippet_leads_with_title_then_truncated_description():
    ev = _event(summary="Design review", description="x" * 500, location="Room 4")
    snippet = _snippet(ev)
    assert snippet is not None
    assert snippet.startswith("Design review — ")
    # Location is not in the snippet; the description is truncated to 200 chars.
    assert "Room 4" not in snippet
    assert snippet == "Design review — " + "x" * 200


def test_snippet_title_only_when_no_description():
    assert _snippet(_event(summary="Standup", location="Room 4")) == "Standup"


def test_location_stays_in_content():
    # `content` (the searchable body) still carries location + description.
    content = _content(_event(summary="Design review", description="Bring specs", location="Room 4"))
    assert "Room 4" in content
    assert "Bring specs" in content
    assert "Design review" in content
