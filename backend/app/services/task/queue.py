"""In-process FIFO queue for manual task creation and updates.

Two callers enqueue here:

  * `POST /tasks` — fresh manual create. The router writes a synthetic
    raw_input(status="processing"), then enqueues for the worker to run
    the agent extractor and create or update a task.
  * `POST /tasks/open/{raw_input_id}` — manual override of an existing
    raw_input the agent already marked `not_task` / `duplicate`. The
    worker can create or update a task, preserving the prior `agent_trace` under a
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
from app.agent.retrieval import precedent_query_text, search_raw_inputs
from app.db import SessionLocal
from app.events import publish_input, publish_task
from app.db.schemas.task import TaskCreate
from app.services.plan import schedule_task
from app.services.task.split import maybe_split_task
from app.services.event_context import apply_event_context, mentions_event
from app.db.clients import raw_inputs as raw_inputs_store, tasks as tasks_store
from app.db.clients.raw_inputs import SimilarInput
from app.services.input.embedding import candidate_query_text, embed

log = logging.getLogger(__name__)

_QueueItem = tuple[uuid.UUID, dict[str, Any], list[uuid.UUID], uuid.UUID | None]
EXTRACT_PRECEDENT_K = 5

_queue: asyncio.Queue[_QueueItem] | None = None
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


async def enqueue(
    raw_input_id: uuid.UUID,
    user_fields: dict[str, Any],
    context_input_ids: list[uuid.UUID] | None = None,
    followup_task_id: uuid.UUID | None = None,
) -> None:
    """`followup_task_id` routes the item through the thread-follow-up agent
    against that task instead of fresh task extraction."""
    if _queue is None:
        raise RuntimeError("task-creation queue is not running")
    await _queue.put((raw_input_id, user_fields, context_input_ids or [], followup_task_id))


async def _run(queue: asyncio.Queue[_QueueItem]) -> None:
    while True:
        raw_input_id, user_fields, context_input_ids, followup_task_id = await queue.get()
        try:
            await _process(raw_input_id, user_fields, context_input_ids, followup_task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let one bad item kill the worker
            log.exception("task-creation failed · raw=%s", raw_input_id)
            _mark_failed(raw_input_id, exc)
        finally:
            queue.task_done()


async def _process(
    raw_input_id: uuid.UUID,
    user_fields: dict[str, Any],
    context_input_ids: list[uuid.UUID],
    followup_task_id: uuid.UUID | None = None,
) -> None:
    with SessionLocal() as session:
        raw = raw_inputs_store.get(session, raw_input_id)
        if raw is None:
            log.warning("task-creation · raw_input missing id=%s", raw_input_id)
            return
        if followup_task_id is not None:
            task = tasks_store.get(session, followup_task_id)
            if task is not None:
                from app.agent.thread.runner import run_thread_followup

                trace = await run_thread_followup(session, raw, task, require_change=True)
                publish_input(session, raw.id)
                affected = trace.get("task_id") or trace.get("existing_task_id") or task.id
                publish_task(session, uuid.UUID(str(affected)))
                return
            # The target vanished between enqueue and processing — detach the
            # stale link and fall through to fresh creation.
            if raw.task_id == followup_task_id:
                raw.task_id = None
                session.commit()
        if raw.task_id is not None:
            log.debug("task-creation · task already linked id=%s", raw_input_id)
            return

        # `processed_at is not None` means the agent already decided about this
        # row (typically `not_task` or `duplicate`) and the user is overriding
        # that decision. Preserve the original trace so we keep an audit trail
        # of both decisions.
        is_override = raw.processed_at is not None

        needs_agent = not all(user_fields.get(k) for k in ("title", "estimation", "due_date"))
        if getattr(raw, "source", None) == "manual" and not user_fields.get("related_event_id"):
            needs_agent = needs_agent or mentions_event(
                " ".join(str(value or "") for value in (
                    user_fields.get("title"), user_fields.get("description"),
                    getattr(raw, "content", None)
                ))
            )
        agent_fields: dict = {}
        agent_trace: dict[str, Any] = {"branch": "manual"}
        if needs_agent:
            precedent_candidates = []
            try:
                context_inputs = _load_context_inputs(session, raw_input_id, context_input_ids)
                precedent_candidates = await _collect_extraction_precedents(
                    session, raw, raw_input_id
                )
                extract_kwargs: dict[str, Any] = {
                    "context_inputs": context_inputs,
                    "include_trace": True,
                }
                if getattr(raw, "source", None) == "manual" and not is_override:
                    extract_kwargs["research"] = False
                if precedent_candidates:
                    extract_kwargs["precedent_candidates"] = precedent_candidates
                extraction = await extract_task_fields(session, raw, **extract_kwargs)
                if isinstance(extraction, tuple):
                    agent_fields, agent_trace = extraction
                else:
                    agent_fields = extraction
            except Exception:  # noqa: BLE001 — fall back only when no existing task could be affected
                if getattr(raw, "source", None) != "manual" or is_override or precedent_candidates:
                    raise
                log.exception("manual extraction failed · raw=%s; using input as title", raw_input_id)
                agent_trace = {"branch": "manual", "extraction_fallback": True}

        action = agent_trace.get("action")
        if action == "update_task":
            from app.agent.helpers.dispatch import apply_task_action

            target_id = uuid.UUID(str(agent_fields.pop("existing_task_id")))
            task = tasks_store.get(session, target_id)
            if task is None:
                raise LookupError(f"Candidate task {target_id} no longer exists")
            action_fields = {
                **agent_fields,
                **user_fields,
                "manually_set_duration": (user_fields.get("estimation") or 0) > 180,
                "existing_task_id": str(target_id),
                "reason": agent_trace.get("reason"),
                "confidence": agent_trace.get("confidence"),
            }
            result = await apply_task_action(session, task, action, action_fields)
            raw.status = "duplicate"
            raw.task_id = target_id
            raw.processed_at = raw.processed_at or datetime.now(timezone.utc)
            manual_trace = {
                **agent_trace,
                **result,
                "task_id": str(target_id),
                "agent_extracted": sorted(agent_fields.keys()),
                "user_provided": sorted(user_fields.keys()),
            }
            _attach_task_action_ref(manual_trace, target_id, action, result["outcome"])
            if is_override:
                trace = dict(raw.agent_trace or {})
                trace["manual_override"] = manual_trace
                raw.agent_trace = trace
            else:
                raw.agent_trace = manual_trace
            session.commit()
            publish_task(session, target_id)
            publish_input(session, raw_input_id)
            return

        extracted_title = agent_fields.get("title")
        if getattr(raw, "source", None) == "manual" and not is_override and not (
            user_fields.get("title") or (isinstance(extracted_title, str) and extracted_title.strip())
        ):
            agent_fields["title"] = (raw.content or "Task").strip()[:512] or "Task"
            agent_trace["extraction_fallback"] = True

        merged = {**agent_fields, **user_fields}
        merged, event_warning = apply_event_context(
            session, merged, explicit_due=user_fields.get("due_date") is not None
        )
        if event_warning:
            agent_trace["event_warning"] = event_warning
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
                related_event_id=merged.get("related_event_id"),
                related_event_calendar_id=merged.get("related_event_calendar_id"),
                related_event_due_derived=bool(merged.get("related_event_due_derived")),
            ),
        )

        if getattr(raw, "source", None) == "manual" and getattr(raw, "embedding", None) is None:
            try:
                raw.embedding = await embed(candidate_query_text(
                    raw.content or "", raw.source_metadata or {}
                ))
            except Exception:
                log.exception("manual task input embedding failed · raw=%s", raw.id)

        raw.status = "open"
        raw.task_id = task.id
        raw.processed_at = raw.processed_at or datetime.now(timezone.utc)

        override_entry = {
            "outcome": "task_created",
            "task_id": str(task.id),
            "agent_extracted": sorted(agent_fields.keys()) if agent_fields else [],
            "user_provided": sorted(user_fields.keys()),
        }
        manual_trace = {**agent_trace, **override_entry, "scheduling": "pending"}
        _attach_task_action_ref(manual_trace, task.id, "create_task", "task_created")
        if is_override:
            trace = dict(raw.agent_trace or {})
            trace["manual_override"] = manual_trace
            raw.agent_trace = trace
        else:
            raw.agent_trace = manual_trace

        session.commit()
        # Creation is complete even if calendar scheduling is slow or fails.
        publish_task(session, task.id)
        publish_input(session, raw_input_id)
        try:
            children = (
                [] if (user_fields.get("estimation") or 0) > 180
                else await maybe_split_task(session, task)
            )
            if not children:
                await schedule_task(session, task)
        except Exception:
            log.exception("task scheduling failed · task=%s", task.id)
        manual_trace["scheduling"] = (
            "not_needed" if getattr(task, "due_date", merged.get("due_date")) is None else
            "scheduled" if getattr(task, "scheduled_date", None) is not None and getattr(task, "calendar_event_id", None) else
            "failed"
        )
        if is_override:
            trace = dict(raw.agent_trace or {})
            trace["manual_override"] = manual_trace
            raw.agent_trace = trace
        else:
            raw.agent_trace = manual_trace
        session.commit()
        publish_input(session, raw_input_id)
        publish_task(session, task.id)


def _load_context_inputs(
    session, anchor_id: uuid.UUID, context_input_ids: list[uuid.UUID]
) -> list:
    """Load the sibling inputs whose content should feed extraction.

    Skips the anchor itself and any id that no longer resolves to a row.
    Order doesn't matter — the extractor sorts the thread by `received_at`."""
    out = []
    seen: set[uuid.UUID] = {anchor_id}
    for cid in context_input_ids:
        if cid in seen:
            continue
        seen.add(cid)
        row = raw_inputs_store.get(session, cid)
        if row is not None:
            out.append(row)
    return out


async def _collect_extraction_precedents(
    session, raw, raw_input_id: uuid.UUID
) -> list[SimilarInput]:
    """Find prior similar decisions for manual/promoted extraction.

    Fresh manual rows are not embedded by the synchronous create path, so this
    computes and stores the same canonical embedding the ingestion pipeline uses.
    KOTX rows stay out of similarity lookup by design.
    """
    if getattr(raw, "source", None) == "kotx":
        return []

    query_embedding = getattr(raw, "embedding", None)
    if query_embedding is None:
        query_text = candidate_query_text(
            getattr(raw, "content", "") or "",
            getattr(raw, "source_metadata", None) or {},
        )
        if not query_text:
            return []
        query_embedding = await embed(query_text)
        if query_embedding is None:
            log.info(
                "task-creation · raw=%s no embedding available, skipping precedents",
                raw_input_id,
            )
            return []
        raw_inputs_store.set_embedding(session, raw_input_id, query_embedding)

    hits = search_raw_inputs(
        session,
        embedding=query_embedding,
        query=precedent_query_text(raw),
        exclude_id=raw_input_id,
        statuses=["open", "closed"],
        k=EXTRACT_PRECEDENT_K,
    )
    return [hit for hit in hits if hit.task_id and hit.status in ("open", "closed")]


def _attach_task_action_ref(
    trace: dict[str, Any], task_id: uuid.UUID, action: str, outcome: str
) -> None:
    """Annotate the selected terminal tool with the task it acted on."""
    ref = f"task:{task_id}"
    for iteration in trace.get("iterations") or []:
        if not isinstance(iteration, dict):
            continue
        for result in iteration.get("tool_results") or []:
            if not isinstance(result, dict) or result.get("name") != action:
                continue
            result["changed_state"] = True
            refs = list(result.get("artifact_refs") or [])
            if ref not in refs:
                refs.append(ref)
            result["artifact_refs"] = refs
            summary = (
                f"created task {task_id}"
                if action == "create_task"
                else f"{outcome.replace('_', ' ')} task {task_id}"
            )
            result["preview"] = summary
            result["result_markdown"] = summary
            result["result_summary"] = summary


def _mark_failed(raw_input_id: uuid.UUID, error: Exception | None = None) -> None:
    """Make sure clients polling on this raw_input don't spin forever.

    For a fresh manual create (processed_at=None) — flip to `not_task` so
    the poll loop exits with a terminal state. For an override (already
    processed by the agent) — keep the prior status untouched and just
    annotate the trace, so the agent's earlier decision isn't lost.
    """
    try:
        with SessionLocal() as session:
            raw = raw_inputs_store.get(session, raw_input_id)
            if raw is None:
                return
            trace = dict(raw.agent_trace or {})
            attempt_trace = getattr(error, "trace", None)
            if not isinstance(attempt_trace, dict):
                attempt_trace = {}
            pending_override = (trace.get("manual_override") or {}).get("outcome") == "processing"
            if raw.task_id is not None and not pending_override:
                return
            if raw.processed_at is None:
                raw.status = "not_task"
                raw.processed_at = datetime.now(timezone.utc)
                trace.update(attempt_trace)
                trace["outcome"] = "task_creation_failed"
            else:
                trace["manual_override"] = {
                    **(trace.get("manual_override") or {}),
                    **attempt_trace,
                    "outcome": "task_creation_failed",
                }
            raw.agent_trace = trace
            session.commit()
            publish_input(session, raw_input_id)
    except Exception:  # noqa: BLE001
        log.exception("failed to mark raw_input failed id=%s", raw_input_id)
