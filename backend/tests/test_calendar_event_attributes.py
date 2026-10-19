from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.db.clients import documents as documents_store  # noqa: E402
from app.db.clients import labels as labels_store  # noqa: E402
from app.services.calendar import attributes as attrs  # noqa: E402
from app.services.calendar import cache as cal_cache  # noqa: E402
from app.services.calendar.client import normalize  # noqa: E402

LABEL_DEFAULTS = {
    "Uni": ("cognitive", "high"),
    "Sport": ("physical", None),
    "Effortful": (None, "high"),
    "Plain": (None, None),
}
LABEL_NAMES = {"lbl-uni": "Uni", "lbl-sport": "Sport", "lbl-eff": "Effortful", "lbl-plain": "Plain"}


def _event(**raw):
    item = {
        "id": "e1",
        "summary": "Lecture",
        "start": {"dateTime": "2026-10-19T09:00:00+02:00"},
        "end": {"dateTime": "2026-10-19T10:30:00+02:00"},
        **raw,
    }
    return normalize(item, "primary")


def _rules(**raw):
    return attrs.rule_attributes(_event(**raw), LABEL_DEFAULTS, LABEL_NAMES)


ME = {"email": "me@example.com", "self": True, "responseStatus": "accepted"}
ALICE = {"email": "alice@example.com", "responseStatus": "needsAction"}
ROOM = {"email": "room@resource.calendar.google.com", "resource": True}


def test_all_day_free_and_managed_events_get_nothing():
    assert _rules(start={"date": "2026-10-19"}, end={"date": "2026-10-20"}) == (None, None, None)
    assert _rules(transparency="transparent", attendees=[ME, ALICE]) == (None, None, None)
    managed = {"private": {"managed_by": "plan_service", "kind": "task"}}
    assert _rules(extendedProperties=managed, eventLabelId="lbl-uni") == (None, None, None)


def test_video_link_or_other_attendees_make_it_social_with_the_label_effort():
    assert _rules(hangoutLink="https://meet.google.com/abc") == ("social", None, "rule")
    assert _rules(conferenceData={"conferenceId": "x"}, eventLabelId="lbl-uni") == (
        "social", "high", "rule",
    )
    assert _rules(location="https://zoom.us/j/123") == ("social", None, "rule")
    assert _rules(attendees=[ME, ALICE], eventLabelId="lbl-sport") == ("social", None, "rule")


def test_only_self_or_rooms_as_attendees_is_not_a_meeting():
    assert _rules(attendees=[ME]) == (None, None, None)
    assert _rules(attendees=[ME, ROOM], eventLabelId="lbl-uni") == ("cognitive", "high", "label")
    assert _rules(location="Room 4.05") == (None, None, None)


def test_label_defaults_apply_otherwise_and_the_rest_is_left_for_the_model():
    assert _rules(eventLabelId="lbl-uni") == ("cognitive", "high", "label")
    assert _rules(eventLabelId="lbl-sport") == ("physical", None, "label")
    assert _rules(eventLabelId="lbl-eff") == (None, "high", "label")
    assert _rules(eventLabelId="lbl-plain") == (None, None, None)
    assert _rules(eventLabelId="lbl-deleted") == (None, None, None)
    assert _rules() == (None, None, None)


def _cached(event, previous, *, content_changed=False):
    return attrs.cached_attributes(
        event, previous, content_changed=content_changed,
        label_defaults=LABEL_DEFAULTS, label_names=LABEL_NAMES,
    )


MODEL = {"work_type": "cognitive", "effort": "medium", "attr_source": "model"}


def test_model_values_survive_a_recache_while_the_text_is_unchanged():
    assert _cached(_event(), MODEL) == MODEL
    assert _cached(_event(), MODEL, content_changed=True) == {}
    assert _cached(_event(), None, content_changed=True) == {}
    assert _cached(_event(), {"work_type": "social", "attr_source": "rule"}) == {}


def test_rules_and_labels_beat_stale_model_values():
    assert _cached(_event(attendees=[ME, ALICE]), MODEL) == {
        "work_type": "social", "attr_source": "rule",
    }
    assert _cached(_event(eventLabelId="lbl-sport"), MODEL) == {
        "work_type": "physical", "attr_source": "label",
    }
    assert _cached(_event(transparency="transparent"), MODEL) == {}


@pytest.mark.asyncio
async def test_cache_one_event_writes_attributes_and_keeps_model_values(monkeypatch):
    event = _event(description="Chapter 3")
    existing = SimpleNamespace(
        content=cal_cache._content(event),
        embedding=[0.1],
        meta={"event_id": "e1", **MODEL},
    )
    upserts: list[dict] = []
    monkeypatch.setattr(documents_store, "get_by_external_id", lambda *a, **k: existing)
    monkeypatch.setattr(documents_store, "upsert", lambda session, **kw: upserts.append(kw))

    async def no_embed(text):
        raise AssertionError("unchanged text must not be re-embedded")

    monkeypatch.setattr(cal_cache, "embed", no_embed)

    await cal_cache.cache_one_event(None, event, labels=(LABEL_DEFAULTS, LABEL_NAMES))
    meta = upserts[-1]["metadata"]
    assert meta["work_type"] == "cognitive"
    assert meta["effort"] == "medium"
    assert meta["attr_source"] == "model"
    assert meta["free"] is False and meta["all_day"] is False

    await cal_cache.cache_one_event(
        None, _event(description="Chapter 3", eventLabelId="lbl-uni"),
        labels=(LABEL_DEFAULTS, LABEL_NAMES),
    )
    meta = upserts[-1]["metadata"]
    assert (meta["work_type"], meta["effort"], meta["attr_source"]) == ("cognitive", "high", "label")


def test_event_tool_parsing_keeps_only_work_type_and_effort():
    tool_input = {"events": [
        {"n": 1, "work_type": "Social", "effort": "low", "deadline_kind": "hard"},
        {"n": "2", "work_type": "cognitive", "effort": "extreme"},
        {"n": 3, "work_type": "nope", "effort": "nope"},
        {"n": 9, "work_type": "physical", "effort": "low"},
        {"n": "x", "work_type": "physical"},
        "garbage",
    ]}

    assert attrs.CLASSIFIER.parse(tool_input, 3) == {
        1: {"work_type": "social", "effort": "low"},
        2: {"work_type": "cognitive"},
    }
    assert attrs.CLASSIFIER.parse(None, 3) == {}
    assert attrs.CLASSIFIER.parse({"tasks": [{"n": 1, "work_type": "social"}]}, 3) == {}


def test_event_tool_schema_asks_for_work_type_and_effort():
    tool = attrs.CLASSIFIER.tool
    item = tool["parameters"]["properties"]["events"]["items"]

    assert tool["name"] == "classify_events"
    assert item["required"] == ["n", "work_type", "effort"]
    assert set(item["properties"]) == {"n", "work_type", "effort"}


def test_batch_message_lists_title_facts_and_notes():
    message = attrs.batch_message([
        {"title": "Design review", "label": "Uni", "location": "Room 4", "minutes": 90,
         "notes": "Bring\nspecs"},
        {"title": "Gym", "label": None, "location": None, "minutes": 60, "notes": ""},
    ])

    assert message.splitlines() == [
        "1. Design review",
        "   label: Uni · location: Room 4 · 90 min",
        "   notes: Bring specs",
        "2. Gym",
        "   60 min",
    ]


def test_snapshot_drops_the_title_from_the_snippet():
    starts = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        title="Design review",
        snippet="Design review — Bring specs",
        meta={"label_id": "lbl-uni", "location": "Room 4"},
        starts_at=starts,
        ends_at=starts + timedelta(minutes=45),
    )

    assert attrs._snapshot(row, LABEL_NAMES) == {
        "title": "Design review",
        "notes": "Bring specs",
        "label": "Uni",
        "location": "Room 4",
        "minutes": 45,
    }


class _FakeSession:
    def __init__(self):
        self.flushes = 0

    def flush(self):
        self.flushes += 1


def _rows(count):
    starts = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            title=f"Event {i}", snippet=f"Event {i}", meta={"event_id": f"e{i}"},
            starts_at=starts, ends_at=starts + timedelta(hours=1),
        )
        for i in range(count)
    ]


@pytest.mark.asyncio
async def test_classify_pending_events_writes_model_values_in_batches(monkeypatch):
    rows = _rows(25)
    windows = []

    def unclassified(session, *, since, until):
        windows.append((since, until))
        return rows

    calls = []

    async def classify(message, count, settings):
        calls.append(count)
        return {1: {"work_type": "social", "effort": "low"}, 3: {"effort": "high"}}

    monkeypatch.setattr(documents_store, "calendar_unclassified", unclassified)
    monkeypatch.setattr(labels_store, "names_by_google_id", lambda session: {})
    monkeypatch.setattr(attrs, "CLASSIFIER", SimpleNamespace(classify=classify))
    until = datetime.now(timezone.utc) + timedelta(days=7)
    session = _FakeSession()

    assert await attrs.classify_pending_events(session, until=until) == 4
    assert calls == [20, 5]
    assert windows[0][1] == until
    assert rows[0].meta == {
        "event_id": "e0", "work_type": "social", "effort": "low", "attr_source": "model",
    }
    assert rows[2].meta == {"event_id": "e2", "effort": "high", "attr_source": "model"}
    assert rows[1].meta == {"event_id": "e1"}
    assert rows[20].meta["attr_source"] == "model"
    assert session.flushes == 2


@pytest.mark.asyncio
async def test_classify_pending_events_leaves_a_failed_batch_pending(monkeypatch):
    rows = _rows(2)

    async def boom(message, count, settings):
        raise RuntimeError("llm down")

    monkeypatch.setattr(documents_store, "calendar_unclassified", lambda s, **kw: rows)
    monkeypatch.setattr(labels_store, "names_by_google_id", lambda session: {})
    monkeypatch.setattr(attrs, "CLASSIFIER", SimpleNamespace(classify=boom))

    until = datetime.now(timezone.utc) + timedelta(days=7)
    assert await attrs.classify_pending_events(_FakeSession(), until=until) == 0
    assert [row.meta for row in rows] == [{"event_id": "e0"}, {"event_id": "e1"}]


@pytest.mark.asyncio
async def test_classify_pending_events_skips_the_model_when_nothing_is_pending(monkeypatch):
    async def never(message, count, settings):
        raise AssertionError("no model call without pending events")

    monkeypatch.setattr(documents_store, "calendar_unclassified", lambda s, **kw: [])
    monkeypatch.setattr(attrs, "CLASSIFIER", SimpleNamespace(classify=never))

    until = datetime.now(timezone.utc) + timedelta(days=7)
    assert await attrs.classify_pending_events(_FakeSession(), until=until) == 0


def test_event_attributes_keys_by_calendar_and_event_id(monkeypatch):
    seen = []

    def metadata(session, external_ids):
        seen.append(sorted(external_ids))
        return {
            "primary:e1": {"work_type": "social", "effort": "low", "attr_source": "rule"},
            "work@group.calendar.google.com:e2": {"label_id": "lbl-plain"},
        }

    monkeypatch.setattr(documents_store, "calendar_metadata", metadata)

    result = attrs.event_attributes(None, [
        ("primary", "e1"), ("work@group.calendar.google.com", "e2"), ("primary", "gone"),
    ])

    assert result == {
        ("primary", "e1"): ("social", "low"),
        ("work@group.calendar.google.com", "e2"): (None, None),
    }
    assert seen == [["primary:e1", "primary:gone", "work@group.calendar.google.com:e2"]]


def test_calendar_unclassified_filters_on_metadata_in_postgres():
    captured = []

    class _Session:
        def scalars(self, statement):
            captured.append(statement)
            return []

    since = datetime(2026, 10, 18, tzinfo=timezone.utc)
    assert documents_store.calendar_unclassified(
        _Session(), since=since, until=since + timedelta(days=8)
    ) == []
    compiled = captured[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.count("documents.metadata ->>") == 4
    assert sql.count("IS NULL") == 2
    assert sql.count("IS DISTINCT FROM") == 2
    assert {"work_type", "attr_source", "all_day", "free"} <= set(compiled.params.values())


def test_calendar_metadata_skips_the_query_for_no_refs():
    assert documents_store.calendar_metadata(None, []) == {}
