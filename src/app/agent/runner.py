"""Agent loop: turn a single RawInput into a decision (create / duplicate / skip).

Kept intentionally thin and source-agnostic. The runner:

  1. Loads relevant context (similar past tasks, optional MCP knowledge).
  2. Calls Claude with the system prompt + tool schemas.
  3. Dispatches tool calls to storage operations.
  4. Writes the agent trace back onto the RawInput for audit/replay.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

# TODO: import Anthropic client / claude_agent_sdk once we settle on which to use
# from anthropic import Anthropic
# from claude_agent_sdk import ...


async def process_raw_input(session: Session, raw_input_id: uuid.UUID) -> None:
    """Run the agent over one raw input and persist the outcome.

    Called from the background worker. Idempotent on raw_input_id —
    re-running should not produce duplicate tasks (handled inside the
    duplicate-detection step).
    """
    # TODO: load RawInput by id, short-circuit if already processed
    # TODO: build embedding for the input content
    # TODO: query top-k similar tasks via pgvector and pass into the prompt
    # TODO: (optional) gather MCP context (GitHub / Notion) if enabled
    # TODO: invoke Claude with TOOLS; loop on tool_use blocks until stop_reason == "end_turn"
    # TODO: dispatch tool calls (create_task / mark_duplicate / mark_not_a_task)
    # TODO: write agent_trace + processed_at back onto the RawInput
    raise NotImplementedError


# --- Tool dispatch stubs ------------------------------------------------------
# These are invoked from the tool-use loop. Real impls move into app.storage.*.

async def _tool_search_similar_tasks(session: Session, query: str, k: int = 5) -> list[dict]:
    # TODO: embed `query`, run pgvector `<=>` order-by query against tasks.embedding
    raise NotImplementedError


async def _tool_create_task(session: Session, raw_input_id: uuid.UUID, **fields) -> dict:
    # TODO: validate via TaskCreate, persist Task, return summary dict
    raise NotImplementedError


async def _tool_mark_duplicate(session: Session, raw_input_id: uuid.UUID, existing_task_id: uuid.UUID, reason: str | None = None) -> dict:
    # TODO: write a Feedback row (kind="duplicate_of") and mark raw input processed
    raise NotImplementedError


async def _tool_mark_not_a_task(session: Session, raw_input_id: uuid.UUID, reason: str) -> dict:
    # TODO: mark raw input processed with status="skipped" and persist reason
    raise NotImplementedError
