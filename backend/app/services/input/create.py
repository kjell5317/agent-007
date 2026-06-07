"""Raw-input creation pipeline.

End-to-end "envelope → embedded row → agent" flow for every ingestion
source. Two public functions:

  * `drain(source, session)` — exhaust a source's `fetch()` iterator.
    For each envelope it calls `create_raw_input` (persist + embed) and
    then hands the row off to the agent. Used by every per-provider
    poll routine (Gmail, Slack, …).

  * `create_raw_input(session, envelope)` — single-envelope step:
    persist via `raw_inputs.create`, build the canonical embedding text,
    call `embed`, write the vector back. Exposed separately for future
    callers (e.g. manual submitters) that don't have a source iterator
    but still want the same insert pipeline.

The agent handoff is the section marked `=== Hand off to the agent ===`
inside `drain` — that's the boundary between "the input service has done
its job" and "the agent decides what happens next".
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.agent import process_raw_input
from app.db.clients import raw_inputs
from app.db.models.raw_input import RawInput
from app.db.schemas.raw_input import RawInputCreate
from app.events import publish_input, publish_task
from app.services.input.embedding import candidate_query_text, embed
from app.services.notify import notify_error

log = logging.getLogger(__name__)


async def drain(source, session: Session) -> dict:
    """Iterate a source's `fetch()`, persist+embed+process each envelope.

    Two error surfaces are caught + notified + recorded in the summary
    rather than bubbling up:

      * the source iterator itself (auth / scope / API errors raised by
        `fetch()`)
      * per-message agent errors
    """
    summary: dict = {
        "fetched": 0,
        "agent_runs": 0,
        "tasks_created": 0,
        "skipped": 0,
        "errors": [],
    }
    source_name = getattr(source, "name", type(source).__name__)

    try:
        async for envelope in source.fetch():
            summary["fetched"] += 1
            subject = (envelope.source_metadata or {}).get("subject")
            log.debug(
                "envelope · %s external_id=%s%s",
                envelope.source,
                envelope.external_id,
                f" subject={subject!r}" if subject else "",
            )

            raw = await create_raw_input(session, envelope)

            if raw.processed_at is not None:
                log.debug("skip · already-processed raw=%s", raw.id)
                continue

            # === Hand off to the agent =====================================
            # The row is persisted and (when possible) embedded. From here on
            # the agent owns the decision: create a task, mark duplicate /
            # not_task, update an existing task via the thread shortcut, or
            # no-op. Any failure is logged + notified but does NOT abort the
            # remaining envelopes in this poll.
            try:
                trace = await process_raw_input(session, raw.id)
                summary["agent_runs"] += 1
                outcome = trace.get("outcome")
                if outcome == "task_created":
                    summary["tasks_created"] += 1
                elif outcome in {"duplicate", "not_task", "closed", "no_change", "updated"}:
                    summary["skipped"] += 1
                log.info(
                    "agent · raw=%s outcome=%s task_id=%s",
                    raw.id,
                    outcome,
                    trace.get("task_id") or trace.get("existing_task_id") or "—",
                )
                publish_input(session, raw.id)
                affected_task = trace.get("task_id") or trace.get("existing_task_id")
                if affected_task:
                    publish_task(session, uuid.UUID(str(affected_task)))
            except Exception as exc:  # noqa: BLE001 — best-effort batch processing
                summary["errors"].append(
                    {"external_id": envelope.external_id, "error": str(exc)}
                )
                session.rollback()
                log.exception(
                    "agent error · raw=%s external_id=%s", raw.id, envelope.external_id
                )
                await notify_error(
                    f"Agent error ({envelope.source})",
                    exc,
                    context=f"external_id={envelope.external_id}",
                )
    except Exception as exc:  # noqa: BLE001 — source fetch failed (auth, scope, network)
        session.rollback()
        summary["errors"].append({"source_fetch": str(exc)})
        log.exception(
            "source fetch failed · source=%s fetched=%d", source_name, summary["fetched"]
        )
        await notify_error(
            f"Source fetch error ({source_name})",
            exc,
            context=f"fetched={summary['fetched']} before failure",
        )

    return summary


async def create_raw_input(session: Session, envelope: RawInputCreate) -> RawInput:
    """Persist one envelope and attach its embedding.

    Returns the row with `embedding` set when the embedding provider is
    reachable, or with `embedding=None` when it isn't — the orchestrator
    degrades to "no similarity hits" in that case.
    """
    raw = raw_inputs.create(session, envelope)
    session.commit()

    # `raw_inputs.create` is idempotent: when an envelope's external_id is
    # already on file it returns the existing row, which may already be
    # processed. Skip the embedding work for those — nothing downstream
    # would read it.
    if raw.processed_at is not None:
        return raw

    query_text = candidate_query_text(raw.content, raw.source_metadata or {})
    if not query_text:
        return raw

    log.debug("embed · raw=%s len=%d", raw.id, len(query_text))
    vector = await embed(query_text)
    if vector is None:
        log.warning("embed · raw=%s NO embedding (no api key or empty text)", raw.id)
        return raw

    raw_inputs.set_embedding(session, raw.id, vector)
    session.commit()
    return raw
