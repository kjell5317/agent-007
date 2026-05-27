"""In-process FIFO queue for manual task creation.

Two callers enqueue here:

  * `POST /tasks` — fresh manual create. The router writes a synthetic
    raw_input(status="processing"), then enqueues for the worker to run
    the agent extractor + persist the task.
  * `POST /tasks/open/{raw_input_id}` — manual override of an existing
    raw_input the agent already marked `not_task` / `duplicate`. Same
    worker, but it preserves the prior `agent_trace` under a
    `manual_override` key so we keep both decisions on the row.

The worker distinguishes the two by `raw.processed_at`: `None` means
fresh, set means override.

A single consumer keeps LLM call counts predictable and avoids concurrent
writes to the same raw_input. Queue state is held in process memory — on
restart anything still in flight stays `processing` until the user retries.
Replacing this with a durable RQ-based queue is tracked in CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.agent import extract_task_fields
from app.db import SessionLocal
from app.db.schemas.task import TaskCreate
from app.services.plan import schedule_task
from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store

log = logging.getLogger(__name__)

_queue: asyncio.Queue[tuple[uuid.UUID, dict[str, Any]]] | None = None
_worker: asyncio.Task | None = None


async def start() -> None:
    global _queue, _worker
    if _worker is not None:
        return
    _queue = asyncio.Queue()
    _worker = asyncio.create_task(_run(_queue), name="task-creation-worker")
    log.info("task-creation queue started")


async def stop() -> None:
    global _queue, _worker
    if _worker is None:
        return
    _worker.cancel()
    try:
        await _worker
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _worker = None
    _queue = None
    log.info("task-creation queue stopped")


async def enqueue(raw_input_id: uuid.UUID, user_fields: dict[str, Any]) -> None:
    if _queue is None:
        raise RuntimeError("task-creation queue is not running")
    await _queue.put((raw_input_id, user_fields))


async def _run(queue: asyncio.Queue[tuple[uuid.UUID, dict[str, Any]]]) -> None:
    while True:
        raw_input_id, user_fields = await queue.get()
        try:
            await _process(raw_input_id, user_fields)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let one bad item kill the worker
            log.exception("task-creation failed · raw=%s", raw_input_id)
            _mark_failed(raw_input_id)
        finally:
            queue.task_done()


async def _process(raw_input_id: uuid.UUID, user_fields: dict[str, Any]) -> None:
    with SessionLocal() as session:
        raw = raw_inputs_store.get(session, raw_input_id)
        if raw is None:
            log.warning("task-creation · raw_input missing id=%s", raw_input_id)
            return
        if raw.task_id is not None:
            log.debug("task-creation · task already linked id=%s", raw_input_id)
            return

        # `processed_at is not None` means the agent already decided about this
        # row (typically `not_task` or `duplicate`) and the user is overriding
        # that decision. Preserve the original trace so we keep an audit trail
        # of both decisions.
        is_override = raw.processed_at is not None

        needs_agent = not all(user_fields.get(k) for k in ("title", "estimation", "due_date"))
        agent_fields: dict = {}
        if needs_agent:
            agent_fields = await extract_task_fields(session, raw)

        merged = {**agent_fields, **user_fields}
        task = tasks_store.create(
            session,
            TaskCreate(
                title=merged["title"],
                description=merged.get("description"),
                estimation=merged.get("estimation"),
                due_date=merged.get("due_date"),
                location=merged.get("location"),
                link=merged.get("link"),
                label=merged.get("label"),
                ai_doable=merged.get("ai_doable"),
            ),
        )

        raw.status = "open"
        raw.task_id = task.id
        raw.processed_at = raw.processed_at or datetime.now(timezone.utc)

        override_entry = {
            "outcome": "task_created",
            "task_id": str(task.id),
            "agent_extracted": sorted(agent_fields.keys()) if agent_fields else [],
            "user_provided": sorted(user_fields.keys()),
        }
        if is_override:
            trace = dict(raw.agent_trace or {})
            trace["manual_override"] = override_entry
            raw.agent_trace = trace
        else:
            raw.agent_trace = {**override_entry, "branch": "manual"}

        session.commit()
        await schedule_task(session, task)


def _mark_failed(raw_input_id: uuid.UUID) -> None:
    """Make sure clients polling on this raw_input don't spin forever.

    For a fresh manual create (processed_at=None) — flip to `not_task` so
    the poll loop exits with a terminal state. For an override (already
    processed by the agent) — keep the prior status untouched and just
    annotate the trace, so the agent's earlier decision isn't lost.
    """
    try:
        with SessionLocal() as session:
            raw = raw_inputs_store.get(session, raw_input_id)
            if raw is None or raw.task_id is not None:
                return
            trace = dict(raw.agent_trace or {})
            if raw.processed_at is None:
                raw.status = "not_task"
                raw.processed_at = datetime.now(timezone.utc)
                trace["outcome"] = "task_creation_failed"
            else:
                trace["manual_override"] = {"outcome": "task_creation_failed"}
            raw.agent_trace = trace
            session.commit()
    except Exception:  # noqa: BLE001
        log.exception("failed to mark raw_input failed id=%s", raw_input_id)
