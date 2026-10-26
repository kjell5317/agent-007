from __future__ import annotations

import json
import os
from types import SimpleNamespace

import httpx
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import jev  # noqa: E402

SETTINGS = SimpleNamespace(
    typesafe_api_key="ts-key",
    jev_base_url="https://api.typesafe.test",
    jev_model="jev-1.14.0",
    jev_mode="on",
    jev_timeout_seconds=2.0,
)
QUESTIONS = {
    "kind": jev.Choice("What is it?", {"task": "Work to do", "not_task": None}),
    "estimate": jev.Score("How long?", ["short", "medium", "long"]),
    "event": jev.Noul("It is an event.", true="An event", false="Not an event"),
}
BODY = {
    "model": "jev-1.14.0",
    "answers": {
        "kind": {
            "type": "choice",
            "choice": "task",
            "confidence": 0.6,
            "probabilities": {"task": 0.8, "not_task": 0.2},
        },
        "estimate": {
            "type": "score",
            "score": 1.6,
            "confidence": 0.5,
            "legend": {"0": "short", "1": "medium", "2": "long"},
            "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
        },
        "event": {"type": "noul", "noul": 0.12},
    },
    "usage": {"input_tokens": 321, "output_tokens": 9},
}


def _patch_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        jev.httpx,
        "AsyncClient",
        lambda timeout: real_client(transport=httpx.MockTransport(handler), timeout=timeout),
    )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(jev.asyncio, "sleep", no_sleep)


def test_question_json_matches_the_api_shapes():
    assert jev.question_json(QUESTIONS["kind"]) == {
        "type": "choice",
        "instructions": "What is it?",
        "criteria": {"task": "Work to do", "not_task": None},
    }
    assert jev.question_json(QUESTIONS["estimate"]) == {
        "type": "score",
        "instructions": "How long?",
        "criteria": ["short", "medium", "long"],
    }
    assert jev.question_json(QUESTIONS["event"]) == {
        "type": "noul",
        "instructions": "It is an event.",
        "criteria": {"true": "An event", "false": "Not an event"},
    }
    assert jev.question_json(jev.Noul("Plain.")) == {"type": "noul", "instructions": "Plain."}


def test_parse_response_reads_every_answer_type():
    result = jev.parse_response(BODY, QUESTIONS, latency_ms=120)

    assert result.model == "jev-1.14.0"
    assert result.input_tokens == 321
    assert result.answers["kind"].value == "task"
    assert result.answers["kind"].p == pytest.approx(0.8)
    # A Score answers with its most probable level, not the expected value.
    assert result.answers["estimate"].value == "2"
    assert result.answers["event"].value == "no"
    assert result.answers["event"].p_of("yes") == pytest.approx(0.12)
    assert result.trace()["answers"]["kind"] == {"value": "task", "p": 0.8}


def test_parse_response_rejects_a_missing_answer():
    body = {**BODY, "answers": {"kind": BODY["answers"]["kind"]}}
    with pytest.raises(jev.JevUnavailable):
        jev.parse_response(body, QUESTIONS, latency_ms=1)


@pytest.mark.asyncio
async def test_ask_posts_the_pinned_model_with_bearer_auth(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=BODY)

    _patch_transport(monkeypatch, handler)
    result = await jev.ask({"input": "hi"}, QUESTIONS, SETTINGS)

    assert str(seen[0].url) == "https://api.typesafe.test/v1/systemone"
    assert seen[0].headers["Authorization"] == "Bearer ts-key"
    payload = json.loads(seen[0].content)
    assert payload["model"] == "jev-1.14.0"
    assert payload["state"] == {"input": "hi"}
    assert set(payload["questions"]) == {"kind", "estimate", "event"}
    assert result.answers["kind"].value == "task"


@pytest.mark.asyncio
async def test_ask_retries_once_when_overloaded(monkeypatch):
    statuses = iter([529, 200])
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(next(statuses), json=BODY),
    )
    result = await jev.ask("state", QUESTIONS, SETTINGS)
    assert result.answers["estimate"].value == "2"


@pytest.mark.asyncio
async def test_ask_raises_unavailable_without_retrying_client_errors(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"detail": "bad key"})

    _patch_transport(monkeypatch, handler)
    with pytest.raises(jev.JevUnavailable):
        await jev.ask("state", QUESTIONS, SETTINGS)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ask_raises_unavailable_on_connection_errors(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(jev.JevUnavailable):
        await jev.ask("state", QUESTIONS, SETTINGS)


def test_modes_need_a_key():
    assert jev.configured(SETTINGS) and jev.acting(SETTINGS)
    shadow = SimpleNamespace(**{**vars(SETTINGS), "jev_mode": "shadow"})
    assert jev.configured(shadow) and not jev.acting(shadow)
    keyless = SimpleNamespace(**{**vars(SETTINGS), "typesafe_api_key": " "})
    assert not jev.configured(keyless) and not jev.acting(keyless)
    off = SimpleNamespace(**{**vars(SETTINGS), "jev_mode": "off"})
    assert not jev.configured(off)
