"""Cheap batch classification of scheduling attributes.

Shared by the task attribute backfill and the calendar event classifier. With
Jev acting, each item is its own small Jev call (the other items would only
distract it); otherwise the items go out as one numbered list per call and
come back through a single forced tool call on the cheap background route
(Gemini Flash when configured).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.agent.helpers import jev
from app.agent.helpers.attributes import DEADLINE_KINDS, EFFORTS, WORK_TYPES, valid_attributes
from app.agent.helpers.llm import chat, user_message
from app.agent.triage.questions import attribute_questions
from app.agent.tools.schemas import DEADLINE_KIND_DOC, EFFORT_DOC, WORK_TYPE_DOC
from app.config import Settings

log = logging.getLogger(__name__)

BATCH_SIZE = 20
# 20 entries plus Gemini's thinking overflow the default 1024-token cap.
MAX_TOKENS = 4096
JEV_CONCURRENCY = 5

_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "work_type": {"type": "string", "enum": list(WORK_TYPES), "description": WORK_TYPE_DOC},
    "effort": {"type": "string", "enum": list(EFFORTS), "description": EFFORT_DOC},
    "deadline_kind": {
        "type": "string", "enum": list(DEADLINE_KINDS), "description": DEADLINE_KIND_DOC,
    },
}


def numbered(entries: Sequence[str]) -> str:
    return "\n".join(f"{number}. {entry}" for number, entry in enumerate(entries, start=1))


@dataclass(frozen=True)
class BatchClassifier:
    tool_name: str
    description: str
    items: str
    # What one entry is, for the Jev question wording ("task", "calendar event").
    subject: str
    fields: tuple[str, ...]
    system_prompt: str
    trace_name: str

    @property
    def tool(self) -> dict[str, Any]:
        return {
            "name": self.tool_name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    self.items: {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "n": {
                                    "type": "integer",
                                    "description": "The entry's number in the list.",
                                },
                                **{field: _FIELD_SCHEMAS[field] for field in self.fields},
                            },
                            "required": ["n", *self.fields],
                        },
                    },
                },
                "required": [self.items],
            },
        }

    def parse(self, tool_input: Any, count: int) -> dict[int, dict[str, str]]:
        """`{entry number: valid fields}`; unknown numbers and enum values are dropped."""
        entries = tool_input.get(self.items) if isinstance(tool_input, Mapping) else None
        parsed: dict[int, dict[str, str]] = {}
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, Mapping):
                continue
            try:
                number = int(entry.get("n"))
            except (TypeError, ValueError):
                continue
            fields = {k: v for k, v in valid_attributes(entry).items() if k in self.fields}
            if 1 <= number <= count and fields:
                parsed[number] = fields
        return parsed

    async def classify(
        self, entries: Sequence[str], settings: Settings
    ) -> dict[int, dict[str, str]]:
        """`{entry number (1-based): valid fields}` for the listed entries."""
        if jev.acting(settings):
            try:
                return await self._classify_with_jev(entries, settings)
            except jev.JevUnavailable:
                log.warning("%s · jev unavailable, falling back to the LLM", self.trace_name)
        # Same cheap route as the subtask planner: Gemini Flash when configured.
        gemini = bool(settings.gemini_api_key)
        response = await chat(
            [user_message(numbered(entries))],
            settings,
            system_prompt=self.system_prompt,
            tools=[self.tool],
            force_tool=self.tool_name,
            provider="google" if gemini else None,
            model=settings.chat_llm_model if gemini else None,
            thinking_level=settings.chat_thinking_level if gemini else None,
            max_tokens=MAX_TOKENS,
            name=self.trace_name,
        )
        call = next((c for c in response.tool_calls if c.name == self.tool_name), None)
        return self.parse(call.input if call else None, len(entries))

    async def _classify_with_jev(
        self, entries: Sequence[str], settings: Settings
    ) -> dict[int, dict[str, str]]:
        questions = attribute_questions(self.fields, self.subject)
        gate = asyncio.Semaphore(JEV_CONCURRENCY)

        async def one(entry: str) -> dict[str, str]:
            async with gate:
                result = await jev.ask(entry, questions, settings, name=self.trace_name)
            return valid_attributes({name: a.value for name, a in result.answers.items()})

        answers = await asyncio.gather(*(one(entry) for entry in entries))
        return {number: fields for number, fields in enumerate(answers, start=1) if fields}
