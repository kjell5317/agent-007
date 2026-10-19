from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import commute as commute_api  # noqa: E402
from app.auth.middleware import _is_exempt_request  # noqa: E402
from app.db import get_session  # noqa: E402
from app.services.commute import flip as flip_service  # noqa: E402
from app.services.commute import flip_link  # noqa: E402
from app.services.commute.flip import FlipRequest  # noqa: E402
from app.services.commute.flip_link import flip_url, verify_flip  # noqa: E402
from app.services.commute.legs import FAILED_MODE, PlannedLeg  # noqa: E402
from app.services.commute.planner import (  # noqa: E402
    _description_for,
    _without_updated_line,
)

DEPART = datetime(2026, 10, 20, 9, 40, tzinfo=timezone.utc)
ARRIVE = DEPART + timedelta(minutes=20)


def _settings(**overrides):
    base = {
        "notify_action_secret": "s3cret",
        "home_assistant_action_secret": "",
        "notification_action_url": "",
        "task_default_url": "https://007.example.org",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _flip_settings(monkeypatch):
    monkeypatch.setattr(flip_link, "get_settings", lambda: _settings())


def _params(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def test_flip_url_targets_the_other_mode_on_the_backend():
    url = flip_url("home", "ev1", DEPART, ARRIVE, "bicycling")

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://007.example.org/commute/flip"
    params = _params(url)
    assert params["origin"] == "home"
    assert params["dest"] == "ev1"
    assert params["mode"] == "transit"
    assert params["depart"] == str(int(DEPART.timestamp()))
    assert params["expires"] == str(int((ARRIVE + timedelta(hours=1)).timestamp()))
    assert _params(flip_url("home", "ev1", DEPART, ARRIVE, "transit"))["mode"] == "bicycling"


def test_flip_url_is_deterministic():
    assert flip_url("home", "ev1", DEPART, ARRIVE, "transit") == flip_url(
        "home", "ev1", DEPART, ARRIVE, "transit"
    )


def test_failed_legs_get_no_link():
    assert flip_url("home", "ev1", DEPART, ARRIVE, FAILED_MODE) is None


def test_no_link_without_base_url_or_secret(monkeypatch):
    monkeypatch.setattr(flip_link, "get_settings", lambda: _settings(task_default_url=""))
    assert flip_url("home", "ev1", DEPART, ARRIVE, "transit") is None

    monkeypatch.setattr(flip_link, "get_settings", lambda: _settings(notify_action_secret=""))
    assert flip_url("home", "ev1", DEPART, ARRIVE, "transit") is None


def test_falls_back_to_home_assistant_secret(monkeypatch):
    monkeypatch.setattr(
        flip_link,
        "get_settings",
        lambda: _settings(notify_action_secret="", home_assistant_action_secret="ha"),
    )
    url = flip_url("home", "ev1", DEPART, ARRIVE, "transit")

    assert verify_flip(_params(url), DEPART).mode == "bicycling"


@pytest.mark.parametrize(
    ("action_url", "expected"),
    [
        ("https://api.example.org/notifications/actions", "https://api.example.org/commute/flip"),
        ("https://x.example.org/app/notifications/actions/", "https://x.example.org/app/commute/flip"),
        ("https://hooks.example.org/ntfy", "https://hooks.example.org/commute/flip"),
    ],
)
def test_endpoint_follows_notification_action_url(monkeypatch, action_url, expected):
    monkeypatch.setattr(
        flip_link, "get_settings", lambda: _settings(notification_action_url=action_url),
    )
    url = flip_url("home", "ev1", DEPART, ARRIVE, "transit")

    assert url.split("?")[0] == expected


def test_endpoint_keeps_public_url_path(monkeypatch):
    monkeypatch.setattr(
        flip_link, "get_settings", lambda: _settings(task_default_url="https://x.example.org/app/"),
    )

    assert flip_url("home", "ev1", DEPART, ARRIVE, "transit").split("?")[0] == (
        "https://x.example.org/app/commute/flip"
    )


def test_verify_round_trip():
    url = flip_url("ev1", "home", DEPART, ARRIVE, "bicycling")

    request = verify_flip(_params(url), ARRIVE)

    assert request == FlipRequest(origin_anchor="ev1", dest_anchor="home", depart=DEPART, mode="transit")


def test_verify_rejects_expired_link():
    params = _params(flip_url("home", "ev1", DEPART, ARRIVE, "transit"))

    verify_flip(params, ARRIVE + timedelta(hours=1))
    with pytest.raises(ValueError, match="expired"):
        verify_flip(params, ARRIVE + timedelta(hours=1, seconds=1))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "ev2"),
        ("dest", "home"),
        ("depart", "0"),
        ("mode", "transit"),
        ("expires", "9999999999"),
        ("sig", "0" * 64),
        ("sig", "é"),
    ],
)
def test_verify_rejects_tampering(field, value):
    params = _params(flip_url("home", "ev1", DEPART, ARRIVE, "transit"))
    params[field] = value

    with pytest.raises(ValueError, match="signature"):
        verify_flip(params, DEPART)


def test_verify_rejects_other_secret(monkeypatch):
    params = _params(flip_url("home", "ev1", DEPART, ARRIVE, "transit"))
    monkeypatch.setattr(flip_link, "get_settings", lambda: _settings(notify_action_secret="other"))

    with pytest.raises(ValueError, match="signature"):
        verify_flip(params, DEPART)


def test_verify_rejects_missing_parameter():
    params = _params(flip_url("home", "ev1", DEPART, ARRIVE, "transit"))
    del params["mode"]

    with pytest.raises(ValueError, match="mode"):
        verify_flip(params, DEPART)


def _leg(**changes) -> PlannedLeg:
    base = {
        "origin_anchor": "home",
        "dest_anchor": "ev1",
        "origin": "Home",
        "destination": "Uni",
        "mode": "bicycling",
        "depart": DEPART,
        "arrive": ARRIVE,
    }
    base.update(changes)
    return PlannedLeg(**base)


def test_description_carries_note_and_flip_link():
    lines = _description_for(
        _leg(note="Bike is at Uni since Tue — riding it back", reason="switched by you")
    ).splitlines()

    assert lines[1] == "Reason: switched by you"
    assert lines[2] == "Bike is at Uni since Tue — riding it back"
    switch = next(line for line in lines if line.startswith("Switch to "))
    assert switch == f"Switch to transit: {flip_url('home', 'ev1', DEPART, ARRIVE, 'bicycling')}"
    assert lines[-1].startswith("Updated: ")

    transit = _description_for(_leg(mode="transit")).splitlines()
    assert any(line.startswith("Switch to bike: https://007.example.org/") for line in transit)


def test_failed_leg_description_has_no_flip_link():
    lines = _description_for(_leg(mode=FAILED_MODE)).splitlines()

    assert not any(line.startswith("Switch to ") for line in lines)


def test_description_is_stable_across_replans(monkeypatch):
    from app.services.commute import planner

    first = _description_for(_leg())

    class _Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 1, 1, tzinfo=tz)

    monkeypatch.setattr(planner, "datetime", _Later)
    second = _description_for(_leg())

    assert first != second
    assert _without_updated_line(first) == _without_updated_line(second)


class _Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture
def client_and_session():
    app = FastAPI()
    app.include_router(commute_api.router)
    session = _Session()
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client, session


def _live_link(mode: str = "bicycling") -> str:
    depart = datetime.now(timezone.utc) + timedelta(hours=2)
    return flip_url("home", "ev1", depart, depart + timedelta(minutes=20), mode)


def test_get_shows_confirm_page_without_flipping(monkeypatch, client_and_session):
    client, session = client_and_session
    seen: list[FlipRequest] = []

    async def describe(_session, request):
        seen.append(request)
        return "Bike → Transit for Uni at 09:40?"

    async def apply(*_args):
        raise AssertionError("a GET must never flip")

    monkeypatch.setattr(flip_service, "describe_flip", describe)
    monkeypatch.setattr(flip_service, "apply_flip", apply)
    url = _live_link()

    response = client.get("/commute/flip?" + urlsplit(url).query)

    assert response.status_code == 200
    assert "Bike → Transit for Uni at 09:40?" in response.text
    assert '<form method="post" action="flip">' in response.text
    for key, value in _params(url).items():
        assert f'name="{key}" value="{value}"' in response.text
    assert seen[0].mode == "transit"
    assert session.commits == 0


def test_post_applies_flip(monkeypatch, client_and_session):
    client, session = client_and_session
    applied: list[FlipRequest] = []

    async def apply(_session, request):
        applied.append(request)
        return "Switched to transit."

    monkeypatch.setattr(flip_service, "apply_flip", apply)
    params = _params(_live_link())

    response = client.post(
        "/commute/flip",
        content=urlencode(params),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert "Switched to transit." in response.text
    assert "<form" not in response.text
    assert applied == [verify_flip(params, datetime.now(timezone.utc))]
    assert session.commits == 1


def test_post_with_bad_signature_is_forbidden(monkeypatch, client_and_session):
    client, _ = client_and_session

    async def apply(*_args):
        raise AssertionError("unsigned flips must not apply")

    monkeypatch.setattr(flip_service, "apply_flip", apply)
    params = _params(_live_link())
    params["mode"] = "bicycling"

    response = client.post("/commute/flip", data=params)

    assert response.status_code == 403
    assert "invalid signature" in response.text


def test_get_with_expired_link_is_forbidden(client_and_session):
    client, _ = client_and_session
    url = flip_url("home", "ev1", DEPART - timedelta(days=30), ARRIVE - timedelta(days=30), "transit")

    response = client.get("/commute/flip?" + urlsplit(url).query)

    assert response.status_code == 403
    assert "expired" in response.text


@pytest.mark.parametrize("method", ["get", "post"])
def test_vanished_leg_says_so(monkeypatch, client_and_session, method):
    client, _ = client_and_session

    async def gone(*_args):
        raise LookupError

    monkeypatch.setattr(flip_service, "describe_flip", gone)
    monkeypatch.setattr(flip_service, "apply_flip", gone)
    params = _params(_live_link())

    if method == "get":
        response = client.get("/commute/flip", params=params)
    else:
        response = client.post("/commute/flip", data=params)

    assert response.status_code == 404
    assert "This leg no longer exists" in response.text


def test_page_escapes_message(monkeypatch, client_and_session):
    client, _ = client_and_session

    async def describe(*_args):
        return "<script>alert(1)</script>"

    monkeypatch.setattr(flip_service, "describe_flip", describe)

    response = client.get("/commute/flip", params=_params(_live_link()))

    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_only_the_flip_path_skips_login():
    assert _is_exempt_request("GET", "/commute/flip")
    assert _is_exempt_request("POST", "/commute/flip")
    assert not _is_exempt_request("GET", "/commute/other")
    assert not _is_exempt_request("GET", "/commute")
