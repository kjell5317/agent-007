"""Tool schemas exposed to the agent.

These are the function signatures Claude will see during tool use. The actual
implementations live in `app.agent.runner` (or, later, in domain services that
the runner dispatches to).

All three tools are terminal: the runner short-circuits after the first one
fires. Dedup candidates are pre-fetched into the user message rather than
discovered via a tool call, to keep per-input cost at one LLM round-trip.
"""

# Anthropic tool-use schema format.
# https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview

TOOLS = [
    {
        "name": "create_task",
        "description": "Persist a new task extracted from the current raw input.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "estimated_minutes": {"type": "integer"},
                "due_at": {"type": "string", "description": "ISO 8601 timestamp"},
                "location": {"type": "string"},
                "source_links": {"type": "array", "items": {"type": "string"}},
                "confidence": {
                    "type": "number",
                    "description": "0.0 - 1.0 self-rated confidence in the extraction.",
                },
            },
            "required": ["title", "confidence"],
        },
    },
    {
        "name": "mark_duplicate",
        "description": (
            "Record that the current input duplicates an existing task instead of "
            "creating a new one. `existing_task_id` must be one of the candidate IDs "
            "listed in the user message."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "existing_task_id": {"type": "string", "format": "uuid"},
                "reason": {"type": "string"},
            },
            "required": ["existing_task_id"],
        },
    },
    {
        "name": "mark_not_a_task",
        "description": "Record that the current input is not actionable for the user.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]
