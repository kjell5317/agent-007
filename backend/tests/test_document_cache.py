from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.services.calendar import cache as cal_cache  # noqa: E402
from app.services.calendar.client import CalendarEvent  # noqa: E402
from app.services.kotx import cache as kotx_cache  # noqa: E402


def _event(**overrides) -> CalendarEvent:
    base = dict(
        id="e1",
        calendar_id="primary",
        summary="Q3 offsite planning",
        description="agenda: budget review",
        start=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        end=datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc),
        all_day=False,
        location="Room 4.05",
        html_link="https://cal/e1",
        private_properties={},
        raw={"updated": "2026-07-01T10:00:00Z"},
    )
    base.update(overrides)
    return CalendarEvent(**base)


def test_calendar_content_joins_summary_location_description():
    assert cal_cache._content(_event()) == "Q3 offsite planning\nRoom 4.05\nagenda: budget review"


def test_calendar_content_drops_missing_parts():
    ev = _event(location=None, description=None)
    assert cal_cache._content(ev) == "Q3 offsite planning"


def test_calendar_metadata_and_external_id():
    ev = _event()
    meta = cal_cache._metadata(ev)
    assert meta["event_id"] == "e1"
    assert meta["calendar_id"] == "primary"
    assert meta["location"] == "Room 4.05"
    assert cal_cache._external_id(ev) == "primary:e1"


def test_calendar_updated_at_parses_google_timestamp():
    assert cal_cache._updated_at(_event()) == datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def test_calendar_updated_at_falls_back_to_now():
    ev = _event(raw={})
    assert cal_cache._updated_at(ev).tzinfo is not None  # a real datetime, not a crash


def _kotx_task(**overrides) -> dict:
    base = {
        "id": 42,
        "repo": "owner/repo",
        "subjectNumber": 7,
        "title": "Fix the flaky test",
        "kind": "implement",
        "state": "done",
        "githubUrl": "https://github.com/owner/repo/pull/9",
    }
    base.update(overrides)
    return base


def test_kotx_title():
    assert kotx_cache._title(_kotx_task()) == "owner/repo#7 Fix the flaky test"


def test_kotx_content_sections():
    content = kotx_cache._content(
        _kotx_task(),
        task_md="Implement the fix by patching X.",
        review_md=None,
        pr={"title": "Fix flaky test", "body": "Adds a retry."},
    )
    assert content.startswith("# owner/repo#7 Fix the flaky test")
    assert "Implement the fix by patching X." in content
    assert "## Proposed PR" in content
    assert "Fix flaky test" in content
    assert "Adds a retry." in content


def test_kotx_content_empty_when_nothing_fetched():
    # Only the title heading would remain; that still counts as content, but a
    # task with no repo/number/title and no docs yields just the fallback.
    content = kotx_cache._content(
        {"id": 5, "kind": "implement"}, task_md=None, review_md=None, pr=None
    )
    assert "kotx task 5" in content


def test_kotx_metadata_filters_none():
    meta = kotx_cache._metadata(_kotx_task(prNumber=9))
    assert meta["kotx_task_id"] == 42
    assert meta["repo"] == "owner/repo"
    assert meta["pr_number"] == 9
    assert "branch" not in meta
