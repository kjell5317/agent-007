"""Open a task from an existing raw_input.

Used by `POST /tasks/open/{raw_input_id}` when the user wants to act on an
earlier agent decision (`not_task` / `duplicate`). If the raw input or its
context identifies an existing task, run the follow-up path so the input can
update, close, reopen, or no-op that task. Otherwise enqueue fresh task
creation through the shared manual task queue.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.clients import (
    raw_inputs as raw_inputs_store,
    tasks as tasks_store,
)
from app.events import publish_task_removed
from app.services.calendar import delete_task_event
from app.services.task.queue import enqueue


async def open_task_from_input(
    session: Session,
    raw_input_id: uuid.UUID,
    user_fields: dict[str, Any],
    context_input_ids: list[uuid.UUID] | None = None,
    target_task_id: uuid.UUID | None = None,
) -> None:
    """Open an already-processed raw_input as a task action.

    `context_input_ids` are sibling inputs (same thread / follow-up group)
    whose content should also feed the agent's extraction, so a task created
    from a grouped thread captures the whole conversation. The anchor
    (`raw_input_id`) is the row that links to the new task.

    When the anchor, an explicit target, or a context input already identifies
    a task, enqueue the raw input for the thread-follow-up agent instead of
    fresh task creation. That lets the agent update fields, close the task,
    reopen it, or leave it unchanged using the same action dispatcher as
    automatic thread follow-ups — run by the queue worker, like every other
    path behind this endpoint's 202 + poll contract.

    Raises:
      * `LookupError` — no raw_input with that id.
      * `ValueError`  — the raw_input is the anchor of an existing open
        task (status='open' with task_id set), so it can't be promoted
        into a *new* task without orphaning the current one.
    """
    raw = raw_inputs_store.get(session, raw_input_id)
    if raw is None:
        raise LookupError("Raw input not found")

    context_input_ids = context_input_ids or []
    target = _find_followup_target(session, raw, context_input_ids, target_task_id)
    if target is not None:
        # Run through the queue worker like fresh creation: the follow-up is
        # an LLM round-trip, and the endpoint promises 202 + poll. A failure
        # gets marked on the row by the worker, so the poll terminates.
        await enqueue(raw_input_id, user_fields, context_input_ids, followup_task_id=target.id)
        return

    if raw.task_id is not None:
        # If the backlink points at a task that no longer exists, keep the
        # historical override behavior: detach this raw input and let the
        # worker attach a fresh task. The prior agent decision is preserved
        # under `agent_trace.manual_override` by the queue worker.
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
                    publish_task_removed(old_task_id)
        else:
            raise ValueError("Raw input is already linked to a task")
    await enqueue(raw_input_id, user_fields, context_input_ids)


def _find_followup_target(
    session: Session,
    raw,
    context_input_ids: list[uuid.UUID],
    target_task_id: uuid.UUID | None,
):
    if target_task_id is not None:
        task = tasks_store.get(session, target_task_id)
        if task is None:
            raise LookupError("Target task not found")
        return task

    if raw.task_id is not None and _can_treat_linked_raw_as_followup(raw):
        return tasks_store.get(session, raw.task_id)

    linked_context = []
    seen: set[uuid.UUID] = {raw.id}
    for cid in context_input_ids:
        if cid in seen:
            continue
        seen.add(cid)
        row = raw_inputs_store.get(session, cid)
        if row is not None and row.task_id is not None:
            linked_context.append(row)

    linked_context.sort(key=lambda row: row.received_at, reverse=True)
    for row in linked_context:
        task = tasks_store.get(session, row.task_id)
        if task is not None:
            return task
    return None


def _can_treat_linked_raw_as_followup(raw) -> bool:
    outcome = (raw.agent_trace or {}).get("outcome")
    return raw.status in ("duplicate", "not_task") or outcome == "no_change"
