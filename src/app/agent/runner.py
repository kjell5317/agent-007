"""Agent loop: turn a single RawInput into a decision (create / duplicate / skip).

Per-input flow:

  1. Compute the candidate-query text and embed it ONCE. The embedding is
     persisted on the raw_input row so any later step (re-runs, debug,
     analytics) reuses it without another embedding-API call.
  2. Look up similar past inputs. If the closest one is above the auto-decide
     similarity threshold, copy its outcome verbatim — no LLM call.
  3. Otherwise: pre-fetch task dedup candidates (hybrid search) + the top
     past-input precedents, hand both to Claude, dispatch the (terminal) tool
     call, and persist the trace.

Source-agnostic throughout: nothing here knows that the raw input is an email.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlock, ToolParam, ToolUseBlock
from sqlalchemy.orm import Session

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOLS
from app.config import get_settings
from app.embeddings import embed, task_embed_text
from app.models.task import Task
from app.schemas.feedback import FeedbackCreate
from app.schemas.task import TaskCreate
from app.storage import feedback, raw_inputs, tasks
from app.storage.raw_inputs import SimilarInput

MAX_TOOL_ITERATIONS = 4
MAX_TOKENS = 1024
TEMPERATURE = 0.4
DEDUP_CANDIDATES = 10
PRECEDENT_CANDIDATES = 5

TERMINAL_TOOLS = frozenset({"create_task", "mark_duplicate", "mark_not_a_task"})


async def process_raw_input(session: Session, raw_input_id: uuid.UUID) -> dict:
    """Run the agent over one raw input and persist the outcome.

    Idempotent on raw_input_id — re-running a processed input is a no-op.
    Returns a small summary dict (mirrored into RawInput.agent_trace).
    """
    raw = raw_inputs.get(session, raw_input_id)
    if raw is None:
        return {"outcome": "missing"}
    if raw.processed_at is not None:
        return {"outcome": "already_processed", "status": raw.status}

    query_text = _candidate_query_text(raw.content, raw.source_metadata or {})

    # --- embed once and cache on the row -------------------------------------
    query_embedding: list[float] | None = raw.embedding
    if query_embedding is None:
        query_embedding = await embed(query_text)
        if query_embedding is not None:
            raw_inputs.set_embedding(session, raw_input_id, query_embedding)
            session.commit()

    # --- precedent-based auto-decide -----------------------------------------
    precedents: list[SimilarInput] = []
    if query_embedding is not None:
        precedents = raw_inputs.search_similar(
            session,
            embedding=query_embedding,
            exclude_id=raw_input_id,
            k=PRECEDENT_CANDIDATES,
        )

    auto = _try_auto_decide(session, raw_input_id, precedents)
    if auto is not None:
        return auto

    # --- hybrid task-candidate fetch + LLM call ------------------------------
    candidates = tasks.search_similar(
        session, query=query_text, embedding=query_embedding, k=DEDUP_CANDIDATES
    )
    user_content = _build_user_message(
        raw.source, raw.content, raw.source_metadata or {}, candidates, precedents
    )

    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    messages: list[MessageParam] = [{"role": "user", "content": user_content}]
    trace: dict[str, Any] = {
        "iterations": [],
        "outcome": None,
        "candidates": len(candidates),
        "precedents": [_precedent_summary(p) for p in precedents],
        "embedded_query": query_embedding is not None,
    }
    final_status = "processed"

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = await client.messages.create(
            model=settings.claude_model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=_cached_system(),
            tools=_cached_tools(),
            messages=messages,
        )

        iter_log: dict[str, Any] = {
            "stop_reason": resp.stop_reason,
            "blocks": [_block_summary(b) for b in resp.content],
        }
        trace["iterations"].append(iter_log)

        tool_uses = [b for b in resp.content if isinstance(b, ToolUseBlock)]
        if not tool_uses:
            trace["outcome"] = trace["outcome"] or "no_tool_call"
            break

        terminal_hit = False
        for tu in tool_uses:
            result = await _dispatch_tool(session, raw_input_id, tu.name, tu.input or {})
            iter_log.setdefault("tool_calls", []).append(
                {"name": tu.name, "input": tu.input, "result": result}
            )

            if tu.name == "create_task":
                trace["outcome"] = "task_created"
                trace["task_id"] = result.get("task_id")
                terminal_hit = True
            elif tu.name == "mark_duplicate":
                trace["outcome"] = "duplicate"
                trace["existing_task_id"] = result.get("existing_task_id")
                final_status = "skipped"
                terminal_hit = True
            elif tu.name == "mark_not_a_task":
                trace["outcome"] = "not_a_task"
                trace["reason"] = result.get("reason")
                final_status = "skipped"
                terminal_hit = True

        if terminal_hit:
            break

        # Defensive: only non-terminal tools (none today) would feed results back.
        messages.append(
            cast(
                MessageParam,
                {"role": "assistant", "content": [_block_to_dict(b) for b in resp.content]},
            )
        )
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": json.dumps(
                    iter_log["tool_calls"][i]["result"], default=str
                ),
            }
            for i, tu in enumerate(tool_uses)
        ]
        messages.append(cast(MessageParam, {"role": "user", "content": tool_results}))
    else:
        trace["outcome"] = trace["outcome"] or "max_iterations"

    raw_inputs.mark_processed(
        session, raw_input_id, status=final_status, agent_trace=trace
    )
    session.commit()
    return trace


# --- precedent auto-decide ----------------------------------------------------


def _try_auto_decide(
    session: Session, raw_input_id: uuid.UUID, precedents: list[SimilarInput]
) -> dict | None:
    """If the top precedent is above threshold, copy its decision and return.

    Only "safe" decisions are auto-applied — those that produce no new task:
      - not_a_task → mark this input not_a_task too
      - task_created on the precedent → mark this input duplicate of that task
      - duplicate of T → mark this input duplicate of T

    Anything else (no_tool_call, max_iterations, missing outcome) is treated
    as inconclusive and falls through to a fresh LLM call.
    """
    if not precedents:
        return None

    threshold = get_settings().input_dedup_threshold
    top = precedents[0]
    if top.similarity < threshold:
        return None

    prior_outcome = (top.agent_trace or {}).get("outcome")
    if prior_outcome not in {"not_a_task", "task_created", "duplicate"}:
        return None

    trace: dict[str, Any] = {
        "outcome": None,
        "auto_decided": True,
        "precedent_id": str(top.id),
        "precedent_similarity": top.similarity,
        "precedent_outcome": prior_outcome,
    }

    if prior_outcome == "not_a_task":
        prior_reason = (top.agent_trace or {}).get("reason") or "matched earlier input"
        feedback.create(
            session,
            FeedbackCreate(
                raw_input_id=raw_input_id,
                kind="not_a_task",
                note=f"auto: precedent {top.id} (sim={top.similarity:.3f}): {prior_reason}",
            ),
        )
        trace["outcome"] = "not_a_task"
        trace["reason"] = prior_reason
        final_status = "skipped"

    else:
        # task_created or duplicate — link this input to the original task.
        prior_task = (top.agent_trace or {}).get("task_id") or (top.agent_trace or {}).get(
            "existing_task_id"
        )
        if not prior_task:
            return None
        feedback.create(
            session,
            FeedbackCreate(
                task_id=uuid.UUID(prior_task),
                raw_input_id=raw_input_id,
                kind="duplicate_of",
                note=f"auto: precedent {top.id} (sim={top.similarity:.3f})",
            ),
        )
        trace["outcome"] = "duplicate"
        trace["existing_task_id"] = prior_task
        final_status = "skipped"

    raw_inputs.mark_processed(
        session, raw_input_id, status=final_status, agent_trace=trace
    )
    session.commit()
    return trace


def _precedent_summary(p: SimilarInput) -> dict:
    """Compact precedent record for the agent_trace audit log."""
    return {
        "id": str(p.id),
        "similarity": round(p.similarity, 4),
        "status": p.status,
        "outcome": (p.agent_trace or {}).get("outcome"),
        "task_id": (p.agent_trace or {}).get("task_id")
        or (p.agent_trace or {}).get("existing_task_id"),
        "subject": p.subject,
    }


# --- prompt assembly ----------------------------------------------------------


def _cached_system() -> list[dict[str, Any]]:
    """System prompt as a single cache-controlled block."""
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _cached_tools() -> list[ToolParam]:
    """Tools list with cache_control on the final entry (caches everything before it)."""
    out = [dict(t) for t in TOOLS]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return cast(list[ToolParam], out)


def _candidate_query_text(content: str, metadata: dict) -> str:
    """Subject + truncated body — used both as keyword query and embedding text."""
    parts: list[str] = []
    subject = metadata.get("subject")
    if subject:
        parts.append(subject)
    body = (content or "").strip()
    if body:
        parts.append(body[:1500])
    return "\n".join(parts).strip()


def _build_user_message(
    source: str,
    content: str,
    metadata: dict,
    candidates: list[Task],
    precedents: list[SimilarInput],
) -> str:
    lines = [f"Source: {source}"]
    for key in ("from", "to", "subject", "date", "thread_id", "account"):
        val = metadata.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")
    urls = metadata.get("urls") or []
    if urls:
        lines.append(f"URLs: {', '.join(urls)}")

    if candidates:
        lines.append("")
        lines.append("Candidate existing tasks (use `mark_duplicate` if one matches):")
        for c in candidates:
            desc = (c.description or "").strip().replace("\n", " ")
            if len(desc) > 160:
                desc = desc[:160] + "…"
            lines.append(f"- {c.id} | {c.title}" + (f" — {desc}" if desc else ""))

    precedent_lines = [_format_precedent(p) for p in precedents if _format_precedent(p)]
    if precedent_lines:
        lines.append("")
        lines.append("Past similar inputs (precedents — strong signal, follow unless clearly wrong):")
        lines.extend(precedent_lines)

    lines.append("")
    lines.append("Body:")
    lines.append(content.strip() or "(empty)")
    return "\n".join(lines)


def _format_precedent(p: SimilarInput) -> str | None:
    """One-line precedent for the user prompt; None if the precedent isn't decisive."""
    outcome = (p.agent_trace or {}).get("outcome")
    if outcome not in {"not_a_task", "task_created", "duplicate"}:
        return None
    subj = p.subject or "(no subject)"
    sender = p.sender or "?"
    sim = f"{p.similarity:.2f}"
    if outcome == "not_a_task":
        reason = (p.agent_trace or {}).get("reason") or ""
        return f"- sim={sim} | NOT_A_TASK | from {sender} | {subj} | reason: {reason[:120]}"
    if outcome == "task_created":
        tid = (p.agent_trace or {}).get("task_id")
        return f"- sim={sim} | CREATED task {tid} | from {sender} | {subj}"
    if outcome == "duplicate":
        tid = (p.agent_trace or {}).get("existing_task_id")
        return f"- sim={sim} | DUPLICATE_OF task {tid} | from {sender} | {subj}"
    return None


# --- tool dispatch ------------------------------------------------------------


async def _dispatch_tool(
    session: Session, raw_input_id: uuid.UUID, name: str, payload: dict
) -> dict:
    if name == "create_task":
        return await _tool_create_task(session, raw_input_id, payload)
    if name == "mark_duplicate":
        return _tool_mark_duplicate(session, raw_input_id, payload)
    if name == "mark_not_a_task":
        return _tool_mark_not_a_task(session, raw_input_id, payload)
    return {"error": f"unknown tool {name!r}"}


async def _tool_create_task(session: Session, raw_input_id: uuid.UUID, payload: dict) -> dict:
    create = TaskCreate(
        title=payload["title"],
        description=payload.get("description"),
        estimated_minutes=payload.get("estimated_minutes"),
        location=payload.get("location"),
        due_at=payload.get("due_at"),
        source_links=payload.get("source_links") or [],
        confidence=payload.get("confidence"),
        raw_input_id=raw_input_id,
    )
    embedding = await embed(task_embed_text(create.title, create.description))
    row = tasks.create(session, create, embedding=embedding)
    return {"task_id": str(row.id), "title": row.title}


def _tool_mark_duplicate(session: Session, raw_input_id: uuid.UUID, payload: dict) -> dict:
    existing_id = uuid.UUID(payload["existing_task_id"])
    reason = payload.get("reason")
    feedback.create(
        session,
        FeedbackCreate(
            task_id=existing_id,
            raw_input_id=raw_input_id,
            kind="duplicate_of",
            note=reason,
        ),
    )
    return {"existing_task_id": str(existing_id), "reason": reason}


def _tool_mark_not_a_task(session: Session, raw_input_id: uuid.UUID, payload: dict) -> dict:
    reason = payload.get("reason", "")
    feedback.create(
        session,
        FeedbackCreate(
            raw_input_id=raw_input_id,
            kind="not_a_task",
            note=reason,
        ),
    )
    return {"reason": reason}


def _block_summary(block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": (block.text or "")[:500]}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": block.input}
    return {"type": getattr(block, "type", "unknown")}


def _block_to_dict(block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": getattr(block, "type", "unknown")}

