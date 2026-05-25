"""Tool schemas exposed to the agent.

Two contexts, two tool sets:

* **New-input context** (raw_input has no matching thread):
  `create_task`, `mark_duplicate`, `mark_not_task`.

* **Thread-follow-up context** (raw_input matches an existing task by thread_id):
  `update_task`, `close_task`, `no_change`.

All tools are terminal — the runner stops after the first tool call.

The tool sets are built on demand so the `label` field can carry the current
label catalog as a strict enum — Claude can't hallucinate a label that isn't
configured.
"""

from app.labels import load_labels

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


def _label_schema(*, required: bool) -> dict:
    """Build the `label` property from the current label config.

    `required=True` → the label is part of `create_task.required`; the agent
    must pick one or fall back to `mark_not_task`. `required=False` is used
    by `update_task` where omitting the field just means "don't change it".
    Returns an empty dict when no labels are configured, so callers can
    detect and skip the field entirely.
    """
    labels = load_labels()
    if not labels:
        return {}
    lines = [f"- {name}: {label.description}" for name, label in labels.items()]
    return {
        "type": "string",
        "enum": list(labels.keys()),
        "description": (
            ("Pick the single best-fitting label for this task. "
             "If none of these labels plausibly fits, call `mark_not_task` "
             "instead — an input that doesn't match any label is unlikely "
             "to be a real task for the user.\n\n"
             if required else
             "Change the label. Pick one that better fits the task.\n\n")
            + "Available labels:\n" + "\n".join(lines)
        ),
    }


_AI_DOABLE_DESCRIPTION = (
    "Whether a capable AI assistant with normal computer use (browser, email, "
    "file editing, code, web search) could meaningfully do this task on its "
    "own — not just help with it. "
    "`yes` = clearly an AI-doable task (drafting, coding, searching, summarizing, "
    "filling forms). "
    "`no` = clearly requires the user in the physical world or with their unique "
    "credentials/judgement (going somewhere, attending a meeting, signing in person, "
    "personal decisions). "
    "`unsure` = mixed or unclear — for example, a task whose scope you can't "
    "tell from the input."
)

_CREATE_TASK_PROPS: dict = {
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
    "ai_doable": {
        "type": "string",
        "enum": ["yes", "no", "unsure"],
        "description": _AI_DOABLE_DESCRIPTION,
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
}
_CREATE_TASK_REQUIRED = ["title", "estimation", "due_date", "ai_doable"]

_UPDATE_TASK_PROPS: dict = {
    "title": {"type": "string"},
    "description": {"type": "string"},
    "estimation": {"type": "integer"},
    "due_date": {"type": "string", "description": "ISO 8601 timestamp"},
    "ai_doable": {
        "type": "string",
        "enum": ["yes", "no", "unsure"],
        "description": "Change the AI-doable assessment. " + _AI_DOABLE_DESCRIPTION,
    },
    "location": {"type": "string"},
    "link": {
        "type": "string",
        "description": (
            "Source URL. Prefer one from the 'Links:' section of "
            "the user message; only scan the body if that section "
            "is absent."
        ),
    },
}

# Patch the label field into the create / update schemas at import time.
# When labels are unconfigured the field is absent — the agent can't pick
# one and the model is never asked for it.
_create_label = _label_schema(required=True)
if _create_label:
    _CREATE_TASK_PROPS["label"] = _create_label
    _CREATE_TASK_REQUIRED.append("label")

_update_label = _label_schema(required=False)
if _update_label:
    _UPDATE_TASK_PROPS["label"] = _update_label


NEW_INPUT_TOOLS = [
    {
        "name": "create_task",
        "description": "Persist a new task extracted from the current raw input.",
        "input_schema": {
            "type": "object",
            "properties": _CREATE_TASK_PROPS,
            "required": _CREATE_TASK_REQUIRED,
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
            "properties": _UPDATE_TASK_PROPS,
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
