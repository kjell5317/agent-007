"""Orchestrator: pick the right agent flow for a single raw input.

Flow:

  1. If the input has a `thread_id` (e.g. Gmail) AND we've already linked a
     prior raw_input on that thread to a task → run the thread-follow-up agent.
     One LLM call, no embedding.

  2. Otherwise embed the input once (cached on the row), then:
     a. If a past raw_input with status='not_task' is similar enough → auto
        mark this input not_task. Zero LLM calls.
     b. Else if a past raw_input with status='open' (i.e. linked to an open
        task) is similar enough → auto mark this input duplicate of that task.
        Zero LLM calls.
     c. Otherwise → run the new-input agent. One LLM call. Candidates include
        similar past inputs against closed tasks (which usually means "create
        a follow-up task") or no similarity at all.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.agent.input.runner import run_new_input_agent
from app.agent.thread.runner import run_thread_followup
from app.config import get_settings
from app.db.clients import raw_inputs, tasks
from app.db.clients.raw_inputs import SimilarInput

log = logging.getLogger(__name__)

SIMILAR_K = 4


async def process_raw_input(session: Session, raw_input_id: uuid.UUID) -> dict:
    """Run the agent over one raw input and persist the outcome."""
    raw = raw_inputs.get(session, raw_input_id)
    if raw is None:
        log.warning("process · raw_input not found id=%s", raw_input_id)
        return {"outcome": "missing"}
    if raw.processed_at is not None:
        log.debug("process · already-processed raw=%s status=%s", raw_input_id, raw.status)
        return {"outcome": "already_processed", "status": raw.status}

    meta = raw.source_metadata or {}
    thread_id = meta.get("thread_id")
    log.info(
        "process start · raw=%s source=%s thread_id=%s",
        raw_input_id, raw.source, thread_id or "—",
    )

    # --- 1. Thread shortcut --------------------------------------------------
    if thread_id:
        prior = raw_inputs.find_by_thread(
            session,
            raw.source,
            thread_id,
            metadata_filters=_thread_lookup_filters(meta),
        )
        if prior is not None and prior.task_id is not None:
            task = tasks.get(session, prior.task_id)
            if task is not None:
                log.info(
                    "branch=thread_followup · raw=%s task=%s (prior_raw=%s)",
                    raw_input_id, task.id, prior.id,
                )
                return await run_thread_followup(session, raw, task)

    # --- 2. Pull the embedding the input service computed at insert time. ---
    # Missing embedding (no API key / empty text) → degrade to "no similarity
    # hits", run the new-input agent with empty candidate sets.
    query_embedding: list[float] | None = raw.embedding
    if query_embedding is None:
        log.info("orchestrator · raw=%s no embedding on row, skipping similarity", raw_input_id)

    # --- 3. Auto-decide vs not_task / open precedents ------------------------
    settings = get_settings()
    auto_threshold = settings.input_dedup_threshold

    not_task_hits: list[SimilarInput] = []
    open_hits: list[SimilarInput] = []
    closed_hits: list[SimilarInput] = []

    if query_embedding is not None:
        not_task_hits = raw_inputs.search_similar(
            session,
            embedding=query_embedding,
            exclude_id=raw_input_id,
            statuses=["not_task"],
            k=SIMILAR_K,
        )
        open_hits = raw_inputs.search_similar(
            session,
            embedding=query_embedding,
            exclude_id=raw_input_id,
            statuses=["open"],
            k=SIMILAR_K,
        )
        closed_hits = raw_inputs.search_similar(
            session,
            embedding=query_embedding,
            exclude_id=raw_input_id,
            statuses=["closed"],
            k=SIMILAR_K,
        )

    if not_task_hits and not_task_hits[0].similarity >= auto_threshold:
        top = not_task_hits[0]
        reason = (top.agent_trace or {}).get("reason") or "matched earlier not_task input"
        trace = {
            "outcome": "not_task",
            "auto_decided": True,
            "precedent_id": str(top.id),
            "precedent_similarity": round(top.similarity, 4),
            "reason": reason,
        }
        log.info(
            "branch=auto_not_task · raw=%s precedent=%s sim=%.3f (threshold=%.2f)",
            raw_input_id, top.id, top.similarity, auto_threshold,
        )
        raw_inputs.finalize(session, raw_input_id, status="not_task", agent_trace=trace)
        session.commit()
        return trace

    if open_hits and open_hits[0].similarity >= auto_threshold and open_hits[0].task_id:
        top = open_hits[0]
        trace = {
            "outcome": "duplicate",
            "auto_decided": True,
            "precedent_id": str(top.id),
            "precedent_similarity": round(top.similarity, 4),
            "existing_task_id": str(top.task_id),
        }
        log.info(
            "branch=auto_duplicate · raw=%s precedent=%s sim=%.3f task=%s",
            raw_input_id, top.id, top.similarity, top.task_id,
        )
        raw_inputs.finalize(
            session,
            raw_input_id,
            status="duplicate",
            task_id=top.task_id,
            agent_trace=trace,
        )
        session.commit()
        return trace

    # No auto-decide path matched.
    if open_hits or not_task_hits:
        top_not_task = not_task_hits[0].similarity if not_task_hits else 0.0
        top_open = open_hits[0].similarity if open_hits else 0.0
        log.debug(
            "auto-decide miss · raw=%s top_not_task=%.3f top_open=%.3f threshold=%.2f",
            raw_input_id, top_not_task, top_open, auto_threshold,
        )

    # --- 4. Otherwise: new-input agent ---------------------------------------
    return await run_new_input_agent(
        session, raw, open_hits, closed_hits, not_task_hits, query_embedding
    )


def _thread_lookup_filters(meta: dict) -> dict[str, str]:
    """Scope thread matches by stable source metadata when available."""
    return {
        key: value
        for key in ("account", "channel_id")
        if isinstance((value := meta.get(key)), str) and value
    }
