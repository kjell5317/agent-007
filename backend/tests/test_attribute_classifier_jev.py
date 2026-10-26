from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")

from app.agent.helpers import jev  # noqa: E402
from app.agent.helpers.jev import Answer, JevResult  # noqa: E402
from app.services import attribute_classifier  # noqa: E402
from app.services.attribute_classifier import BatchClassifier  # noqa: E402

CLASSIFIER = BatchClassifier(
    tool_name="classify_tasks",
    description="",
    items="tasks",
    subject="task",
    fields=("work_type", "effort"),
    system_prompt="",
    trace_name="test-classify",
)
ACTING = SimpleNamespace(typesafe_api_key="ts-key", jev_mode="on", gemini_api_key="")


@pytest.mark.asyncio
async def test_jev_classifies_each_entry_on_its_own(monkeypatch):
    states = []

    async def ask(state, questions, settings, *, name="jev"):
        states.append(state)
        assert set(questions) == {"work_type", "effort"}
        value = "social" if "Call" in state else "cognitive"
        return JevResult(
            model="jev-1.14.0",
            input_tokens=10,
            latency_ms=5,
            answers={"work_type": Answer(value, 0.9), "effort": Answer("low", 0.7)},
        )

    monkeypatch.setattr(jev, "ask", ask)
    result = await CLASSIFIER.classify(["Call mum", "Write essay\n   label: Uni"], ACTING)

    assert sorted(states) == ["Call mum", "Write essay\n   label: Uni"]
    assert result == {
        1: {"work_type": "social", "effort": "low"},
        2: {"work_type": "cognitive", "effort": "low"},
    }


@pytest.mark.asyncio
async def test_jev_outage_falls_back_to_the_llm_batch(monkeypatch):
    async def down(*args, **kwargs):
        raise jev.JevUnavailable("HTTP 529")

    messages = []

    async def fake_chat(msgs, settings, **kwargs):
        messages.append(msgs[0].text)
        call = SimpleNamespace(
            name="classify_tasks",
            input={
                "tasks": [
                    {"n": 1, "work_type": "social", "effort": "low"},
                ]
            },
        )
        return SimpleNamespace(tool_calls=[call])

    monkeypatch.setattr(jev, "ask", down)
    monkeypatch.setattr(attribute_classifier, "chat", fake_chat)
    result = await CLASSIFIER.classify(["Call mum", "Write essay"], ACTING)

    assert messages == ["1. Call mum\n2. Write essay"]
    assert result == {1: {"work_type": "social", "effort": "low"}}
