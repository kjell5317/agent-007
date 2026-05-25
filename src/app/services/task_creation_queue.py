"""In-process FIFO queue for manual task creation.

`POST /tasks` enqueues `(raw_input_id, user_fields)` and returns immediately
with the raw_input id. A single background worker consumes the queue
serially: loads the raw_input, runs `extract_task_fields` if the user didn't
supply title/estimation/due_date, creates the task, finalizes the raw_input,
and mirrors to Google Calendar.

Single-consumer keeps LLM call counts predictable and avoids concurrent
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

from app.agent.runner import extract_task_fields
from app.db import SessionLocal
from app.schemas.task import TaskCreate
from app.services.google_calendar import add_task_to_calendar
from app.storage import raw_inputs as raw_inputs_store, tasks as tasks_store

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
        if raw.processed_at is not None:
            log.debug("task-creation · already processed id=%s", raw_input_id)
            return

        needs_agent = not all(user_fields.get(k) for k in ("title", "estimation", "due_date"))
        agent_fields: dict = {}
        if needs_agent:
            agent_fields = await extract_task_fields(raw)

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
        raw.processed_at = datetime.now(timezone.utc)
        raw.agent_trace = {
            "outcome": "task_created",
            "branch": "manual",
            "task_id": str(task.id),
            "agent_extracted": sorted(agent_fields.keys()) if agent_fields else [],
            "user_provided": sorted(user_fields.keys()),
        }
        session.commit()
        await add_task_to_calendar(session, task)


def _mark_failed(raw_input_id: uuid.UUID) -> None:
    """Make sure clients polling on this raw_input don't spin forever — flip
    it to `not_task` with a failure trace so the poll loop exits cleanly."""
    try:
        with SessionLocal() as session:
            raw = raw_inputs_store.get(session, raw_input_id)
            if raw is None or raw.processed_at is not None:
                return
            raw.status = "not_task"
            raw.processed_at = datetime.now(timezone.utc)
            trace = dict(raw.agent_trace or {})
            trace["outcome"] = "task_creation_failed"
            raw.agent_trace = trace
            session.commit()
    except Exception:  # noqa: BLE001
        log.exception("failed to mark raw_input failed id=%s", raw_input_id)
