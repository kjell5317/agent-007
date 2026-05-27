"""Open a task from an existing raw_input.

Used by `POST /tasks/open/{raw_input_id}` when the user wants to
override the agent's earlier decision (`not_task` / `duplicate`) and
turn that raw_input into a real task.

The work itself — agent field extraction, task insert, calendar mirror —
runs on the shared task-creation queue so it doesn't block the API
thread and so we keep one place where the LLM is called. The router
returns immediately with the raw_input id; clients poll
`GET /inputs/{raw_input_id}` until the row gains a `task_id`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.clients import (
    raw_inputs as raw_inputs_store,
    tasks as tasks_store,
)
from app.services.calendar import delete_task_event
from app.services.task.queue import enqueue


async def open_task_from_input(
    session: Session,
    raw_input_id: uuid.UUID,
    user_fields: dict[str, Any],
) -> None:
    """Enqueue task creation for an already-processed raw_input.

    Raises:
      * `LookupError` — no raw_input with that id.
      * `ValueError`  — the raw_input is the anchor of an existing open
        task (status='open' with task_id set), so it can't be promoted
        into a *new* task without orphaning the current one.
    """
    raw = raw_inputs_store.get(session, raw_input_id)
    if raw is None:
        raise LookupError("Raw input not found")
    if raw.task_id is not None:
        # `duplicate` (status='duplicate'), `no_change` follow-ups
        # (status='open', trace.outcome='no_change'), and lingering
        # `not_task` rows from a pre-fix dismiss (status='not_task' with
        # task_id still set) all hold a backlink the user can override
        # ("this is actually its own task"). Break the backlink and let
        # the worker attach a fresh task. The prior agent decision is
        # preserved under `agent_trace.manual_override` by the queue
        # worker.
        outcome = (raw.agent_trace or {}).get("outcome")
        if raw.status in ("duplicate", "not_task") or outcome == "no_change":
            old_task_id = raw.task_id
            raw.task_id = None
            session.commit()
            # Defensive: the override should never leave the old task
            # without an anchor (we only detach follow-ups, not the
            # task_created anchor itself). If somehow `raw` was the last
            # non-duplicate row pointing at it, drop the task rather
            # than leaving an orphan — orphans surface as "open" by
            # default in tasks.list_ and have no raw_input for
            # close / dismiss / reopen to flip.
            if raw_inputs_store.latest_for_task(session, old_task_id) is None:
                old_task = tasks_store.get(session, old_task_id)
                if old_task is not None:
                    await delete_task_event(session, old_task)
                    session.delete(old_task)
                    session.commit()
        else:
            raise ValueError("Raw input is already linked to a task")
    await enqueue(raw_input_id, user_fields)
