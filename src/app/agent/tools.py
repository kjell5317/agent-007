"""Tool schemas exposed to the agent.

Two contexts, two tool sets:

* **New-input context** (raw_input has no matching thread):
  `create_task`, `mark_duplicate`, `mark_not_task`.

* **Thread-follow-up context** (raw_input matches an existing task by thread_id):
  `update_task`, `close_task`, `no_change`.

All tools are terminal — the runner stops after the first tool call.
"""

# Anthropic tool-use schema format.
# https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview

_CONFIDENCE_SCHEMA = {
    "type": "number",
    "minimum": 0.0,
    "maximum": 1.0,
    "description": (
        "Your confidence in this decision, between 0.0 and 1.0. Calibrate: "
        "≥0.9 = obvious, 0.7 = likely, 0.5 = coin-flip."
    ),
}

NEW_INPUT_TOOLS = [
    {
        "name": "create_task",
        "description": "Persist a new task extracted from the current raw input.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "estimation": {
                    "type": "integer",
                    "description": "Estimated duration in minutes. Always set a best-guess value.",
                },
                "due_date": {
                    "type": "string",
                    "description": (
                        "ISO 8601 timestamp. Always set: use the explicit deadline if present, "
                        "otherwise a reasonable best-guess based on urgency."
                    ),
                },
                "location": {"type": "string"},
                "link": {
                    "type": "string",
                    "description": (
                        "Most relevant source URL. When the user message contains "
                        "a 'Links:' section, pick one of those — do NOT scan the "
                        "body for URLs unless that section is absent."
                    ),
                },
            },
            "required": ["title", "estimation", "due_date"],
        },
    },
    {
        "name": "mark_duplicate",
        "description": (
            "Record that the current input duplicates an existing task instead of "
            "creating a new one. `existing_task_id` must come from the candidate list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "existing_task_id": {"type": "string", "format": "uuid"},
                "reason": {"type": "string"},
                "confidence": _CONFIDENCE_SCHEMA,
            },
            "required": ["existing_task_id"],
        },
    },
    {
        "name": "mark_not_task",
        "description": "Record that the current input is not actionable for the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "confidence": _CONFIDENCE_SCHEMA,
            },
            "required": ["reason"],
        },
    },
]


THREAD_FOLLOWUP_TOOLS = [
    {
        "name": "update_task",
        "description": (
            "Apply patches to the existing task because the follow-up input adds "
            "new information. Only include fields that should change."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "estimation": {"type": "integer"},
                "due_date": {"type": "string", "description": "ISO 8601 timestamp"},
                "location": {"type": "string"},
                "link": {
                    "type": "string",
                    "description": (
                        "Source URL. Prefer one from the 'Links:' section of "
                        "the user message; only scan the body if that section "
                        "is absent."
                    ),
                },
            },
        },
    },
    {
        "name": "close_task",
        "description": "Mark the linked task as done; the follow-up indicates completion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "confidence": _CONFIDENCE_SCHEMA,
            },
        },
    },
    {
        "name": "no_change",
        "description": "The follow-up adds no actionable change to the existing task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "confidence": _CONFIDENCE_SCHEMA,
            },
        },
    },
]
