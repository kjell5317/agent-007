"""Tool schemas exposed to the agent.

These are the function signatures Claude will see during tool use. The actual
implementations live in `app.agent.runner` (or, later, in domain services that
the runner dispatches to).
"""

# Anthropic tool-use schema format.
# https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview

TOOLS = [
    {
        "name": "search_similar_tasks",
        "description": (
            "Semantic-search existing open tasks. Use BEFORE creating a task to avoid duplicates. "
            "Returns up to k tasks ranked by similarity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Short description of the candidate task."},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
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
        "description": "Record that the current input duplicates an existing task instead of creating a new one.",
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
    # TODO: add `fetch_github_issue`, `fetch_notion_page` etc. once MCP wired
]
