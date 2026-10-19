"""Cheap-model batch classification of scheduling attributes.

Shared by the task attribute backfill and the calendar event classifier: the
items go out as one numbered list per call and come back through a single
forced tool call on the cheap background route (Gemini Flash when configured).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.agent.helpers.attributes import DEADLINE_KINDS, EFFORTS, WORK_TYPES, valid_attributes
from app.agent.helpers.llm import chat, user_message
from app.agent.tools.schemas import DEADLINE_KIND_DOC, EFFORT_DOC, WORK_TYPE_DOC
from app.config import Settings

BATCH_SIZE = 20
# 20 entries plus Gemini's thinking overflow the default 1024-token cap.
MAX_TOKENS = 4096

_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "work_type": {"type": "string", "enum": list(WORK_TYPES), "description": WORK_TYPE_DOC},
    "effort": {"type": "string", "enum": list(EFFORTS), "description": EFFORT_DOC},
    "deadline_kind": {
        "type": "string", "enum": list(DEADLINE_KINDS), "description": DEADLINE_KIND_DOC,
    },
}


@dataclass(frozen=True)
class BatchClassifier:
    tool_name: str
    description: str
    items: str
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
        self, message: str, count: int, settings: Settings
    ) -> dict[int, dict[str, str]]:
        # Same cheap route as the subtask planner: Gemini Flash when configured.
        gemini = bool(settings.gemini_api_key)
        response = await chat(
            [user_message(message)],
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
        return self.parse(call.input if call else None, count)
