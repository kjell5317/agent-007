"""Flipping a leg: pin and re-plan ahead of time, correct the record after."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.calendar.client import normalize
from app.services.commute import flip


def leg_event(mode: str, depart: datetime) -> object:
    return normalize({
        "id": "leg-1",
        "summary": "🚲 Travel",
        "description": "From: Home, Street 1\nNavigate: x",
        "location": "Uni, Campus 2",
        "start": {"dateTime": depart.isoformat()},
        "end": {"dateTime": (depart + timedelta(minutes=20)).isoformat()},
        "extendedProperties": {"private": {
            "managed_by": "plan_service", "kind": "commute",
            "origin_anchor": "home", "dest_anchor": "lec", "mode": mode,
        }},
    }, "primary")


@pytest.fixture
def flips(monkeypatch):
    state = SimpleNamespace(events=[], logged=[], rewritten=[], replans=0)

    async def find(_session, _request):
        return state.events.pop(0) if len(state.events) > 1 else state.events[0]

    async def features(_session, _record, _mode):
        return {"planned": {}, "flipped": {}}

    async def rewrite(_session, _event, record, mode):
        state.rewritten.append((record.dest_anchor, mode))

    async def replan(_session):
        state.replans += 1

    monkeypatch.setattr(flip, "_find_leg", find)
    monkeypatch.setattr(flip, "_features", features)
    monkeypatch.setattr(flip, "_rewrite_past_leg", rewrite)
    monkeypatch.setattr(flip, "_replan", replan)
    monkeypatch.setattr("app.db.clients.commute_log.add", lambda _s, **kw: state.logged.append(kw))
    monkeypatch.setattr("app.services.learn.jobs.refit_soon", lambda: None)
    return state


def request(depart, mode="transit"):
    return flip.FlipRequest(origin_anchor="home", dest_anchor="lec", depart=depart, mode=mode)


def session():
    return SimpleNamespace(commit=lambda: None)


@pytest.mark.asyncio
async def test_future_flip_pins_and_replans(flips):
    depart = datetime.now(timezone.utc) + timedelta(hours=3)
    flips.events = [leg_event("bicycling", depart), leg_event("transit", depart)]
    message = await flip.apply_flip(session(), request(depart))
    assert message == "Switched to transit."
    [row] = flips.logged
    assert (row["planned_mode"], row["flipped_mode"], row["past"]) == ("bicycling", "transit", False)
    assert flips.replans == 1 and flips.rewritten == []


@pytest.mark.asyncio
async def test_unmet_pin_is_reported(flips):
    depart = datetime.now(timezone.utc) + timedelta(hours=3)
    flips.events = [leg_event("transit", depart), leg_event("transit", depart)]
    message = await flip.apply_flip(session(), request(depart, mode="bicycling"))
    assert "bike isn't where this leg starts" in message


@pytest.mark.asyncio
async def test_past_flip_corrects_the_record(flips):
    depart = datetime.now(timezone.utc) - timedelta(hours=2)
    flips.events = [leg_event("transit", depart)]
    message = await flip.apply_flip(session(), request(depart, mode="bicycling"))
    assert flips.logged[0]["past"] is True
    assert flips.rewritten == [("lec", "bicycling")]
    assert "The bike is at Uni" in message


@pytest.mark.asyncio
async def test_describe_and_noop(flips):
    depart = datetime.now(timezone.utc) + timedelta(hours=3)
    flips.events = [leg_event("bicycling", depart)]
    preview = await flip.describe_flip(session(), request(depart))
    assert (preview.current, preview.target, preview.place) == ("bicycling", "transit", "Uni")
    assert await flip.apply_flip(session(), request(depart, mode="bicycling")) == "This leg is already bike."
    assert flips.logged == []
