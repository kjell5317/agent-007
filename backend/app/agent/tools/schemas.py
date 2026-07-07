"""Tool schemas exposed to the agent.

Two contexts, two tool sets:

* **New-input context** (raw_input has no matching thread): `create_task`,
  `mark_not_task`, and — when the input duplicates a candidate task — the
  same `update_task` / `no_change` pair the thread context uses, except here
  each carries `existing_task_id` to name the target.

* **Thread-follow-up context** (raw_input matches an existing task by thread_id):
  `update_task`, `no_change` (target task is implicit).

`update_task` carries an optional `status` (`open` / `closed`) so a single
tool both edits fields and drives the lifecycle: close a task that's done, or
reopen a closed one. `no_change` is genuinely inert — it touches nothing.

All tools are terminal — the runner stops after the first tool call.

The tool sets are built on demand so the `label` field can carry the current
label catalog as a strict enum — the model can't choose a label that isn't
configured.
"""

from app.labels import load_labels

# Provider-neutral JSON schema format; provider adapters translate at the boundary.

_CONFIDENCE_SCHEMA = {
    "type": "number",
    "minimum": 0.0,
    "maximum": 1.0,
    "description": (
        "Your confidence in this decision, between 0.0 and 1.0. Calibrate: "
        "≥0.9 = obvious, 0.7 = likely, 0.5 = coin-flip."
    ),
}

# Every terminal tool carries this: long-term memory is harvested from all
# decisions, not just rejected inputs.
_NOTES_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Zero or more short, self-contained facts worth keeping as long-term "
        "memory (someone's role, an account number, a reference, a policy, a "
        "recurring context). Each entry must stand on its own without the "
        "original input. Future runs retrieve these via `search_notes`. Only "
        "save genuinely useful information; skip ephemeral content."
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


_CREATE_TASK_PROPS: dict = {
    "title": {
        "type": "string",
        "minLength": 3,
        "description": (
            "Short, specific, displayable task title. Do not use placeholders "
            "such as 'No subject' or 'Untitled'."
        ),
    },
    "description": {"type": "string"},
    "estimation": {
        "type": "integer",
        "description": "Estimated duration in minutes. Always set a best-guess value.",
    },
    "due_date": {
        "type": "string",
        "description": (
            "ISO 8601 timestamp. Always set: use the explicit deadline if present, "
            "otherwise a reasonable best-guess based on urgency. Prefer 15-minute "
            "choices (:00, :15, :30, :45); EOD/end of day means 23:45."
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
    "notes": _NOTES_SCHEMA,
}
_CREATE_TASK_REQUIRED = ["title", "estimation", "due_date"]

_UPDATE_TASK_PROPS: dict = {
    "title": {"type": "string"},
    "description": {"type": "string"},
    "estimation": {"type": "integer"},
    "due_date": {
        "type": "string",
        "description": (
            "ISO 8601 timestamp. Prefer 15-minute choices (:00, :15, :30, :45); "
            "EOD/end of day means 23:45. When setting status=open to reopen a "
            "closed task whose current due_date or scheduled_date is in the "
            "past, include a new future due_date unless the input explicitly "
            "says the date should not change."
        ),
    },
    "status": {
        "type": "string",
        "enum": ["open", "closed"],
        "description": (
            "Lifecycle change for the task. `closed` = the task is done or no "
            "longer needed; `open` = reopen a task that was previously closed. "
            "Omit to leave the current state unchanged. Can be combined with "
            "field edits in the same call. When reopening a closed task whose "
            "current due_date or scheduled_date is in the past, include a new "
            "future due_date unless the input explicitly says the date should "
            "not change."
        ),
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
    "notes": _NOTES_SCHEMA,
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

_EXISTING_TASK_ID_SCHEMA = {
    "type": "string",
    "format": "uuid",
    "description": "Id of the matching task — must come from the CANDIDATE TASKS list.",
}


NEW_INPUT_TOOLS = [
    {
        "name": "search_notes",
        "description": (
            "Look up previously saved notes (the agent's long-term memory, "
            "saved from past inputs). Use this before deciding when "
            "the current input references a person, project, account, or fact "
            "you might have recorded earlier. Returns the top matching notes "
            "by semantic similarity. Non-terminal — you can call it more than "
            "once and you still need a terminal tool (`create_task`, "
            "`mark_not_task`, or a duplicate action) to finish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look up. A short phrase or sentence describing "
                        "the entity or fact you're trying to remember."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "find_calendar_events",
        "description": (
            "List events already on the user's primary calendar inside a time "
            "window. Call this before `create_event` to check whether the event "
            "the current input describes is already there, so you don't create a "
            "duplicate. Also call this before `update_event` so you have the "
            "event id to update. Non-terminal — you still need a terminal tool "
            "(`create_task`, `mark_not_task`, or a duplicate action) to finish."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_min": {
                    "type": "string",
                    "description": "ISO 8601 start of the search window (inclusive).",
                },
                "time_max": {
                    "type": "string",
                    "description": "ISO 8601 end of the search window (exclusive).",
                },
            },
            "required": ["time_min", "time_max"],
        },
    },
    {
        "name": "update_event",
        "description": (
            "Patch an existing non-task event on the user's primary calendar. "
            "Use only after `find_calendar_events`, passing the returned "
            "`event_id`, when the current input corrects or reschedules an "
            "existing calendar event. Do not use for task mirrors or commute "
            "events; use `update_task` for task-related changes. Include only "
            "the fields that should change. Non-terminal: updating the event "
            "does NOT finish the run; follow up with a terminal tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "Calendar event id returned by `find_calendar_events`.",
                },
                "summary": {"type": "string", "description": "Updated event title."},
                "start": {
                    "type": "string",
                    "description": (
                        "Updated ISO 8601 start. Use the user's local zone unless "
                        "the input names another. If only start changes, the "
                        "existing event duration is preserved."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": "Updated ISO 8601 end. Must be after start.",
                },
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "create_task",
        "description": "Persist a new task extracted from the current raw input.",
        "parameters": {
            "type": "object",
            "properties": _CREATE_TASK_PROPS,
            "required": _CREATE_TASK_REQUIRED,
        },
    },
    {
        "name": "create_event",
        "description": (
            "Add an event to the user's primary calendar — use for invitations, "
            "appointments, talks, or announcements the user should see on their "
            "calendar but that are not themselves tasks. Check "
            "`find_calendar_events` first so you don't duplicate an event that "
            "already exists. Non-terminal: creating the event does NOT finish "
            "the run. If attending requires no action from the user, follow up "
            "with `mark_not_task`; if it needs registration or preparation, also "
            "call `create_task` for that work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title."},
                "start": {
                    "type": "string",
                    "description": (
                        "ISO 8601 start. Use the user's local zone unless the "
                        "input names another."
                    ),
                },
                "end": {
                    "type": "string",
                    "description": (
                        "ISO 8601 end. If the input doesn't state one, omit it "
                        "and a default duration is applied."
                    ),
                },
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["summary", "start"],
        },
    },
    {
        "name": "no_change",
        "description": (
            "The current input duplicates one of the CANDIDATE TASKS and adds "
            "nothing new — e.g. the same message arriving again or from another "
            "source. Record the duplicate and leave the task untouched. This is "
            "the common duplicate case."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "existing_task_id": _EXISTING_TASK_ID_SCHEMA,
                "reason": {"type": "string"},
                "confidence": _CONFIDENCE_SCHEMA,
                "notes": _NOTES_SCHEMA,
            },
            "required": ["existing_task_id"],
        },
    },
    {
        "name": "update_task",
        "description": (
            "The current input refers to one of the CANDIDATE TASKS and changes "
            "it. Patch the fields that change and/or set `status` to drive the "
            "lifecycle: `closed` when it's done or cancelled, `open` to reopen a "
            "CLOSED candidate the input revives. If reopening a closed task whose "
            "current due_date or scheduled_date is in the past, include a new "
            "future due_date unless the input explicitly says the date should not "
            "change. Include only what changes."
        ),
        "parameters": {
            "type": "object",
            "properties": {**_UPDATE_TASK_PROPS, "existing_task_id": _EXISTING_TASK_ID_SCHEMA},
            "required": ["existing_task_id"],
        },
    },
    {
        "name": "mark_not_task",
        "description": (
            "Record that the current input is not actionable for the user. "
            "Optionally include `notes` — short standalone facts worth keeping "
            "as long-term memory. Future agent runs can retrieve these via "
            "`search_notes`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "confidence": _CONFIDENCE_SCHEMA,
                "notes": _NOTES_SCHEMA,
            },
            "required": ["reason"],
        },
    },
]


THREAD_FOLLOWUP_TOOLS = [
    {
        "name": "update_task",
        "description": (
            "The follow-up changes the existing task. Patch the fields that "
            "change and/or set `status`: `closed` when the follow-up indicates "
            "completion or cancellation, `open` to reopen a task that was closed. "
            "If reopening a closed task whose current due_date or scheduled_date "
            "is in the past, include a new future due_date unless the follow-up "
            "explicitly says the date should not change. Include only what changes."
        ),
        "parameters": {
            "type": "object",
            "properties": _UPDATE_TASK_PROPS,
        },
    },
    {
        "name": "no_change",
        "description": "The follow-up adds no actionable change to the existing task.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "confidence": _CONFIDENCE_SCHEMA,
                "notes": _NOTES_SCHEMA,
            },
        },
    },
]
