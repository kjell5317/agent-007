"""Agent loop: turn one raw input into a decision.

Flow per raw input:

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
from datetime import datetime
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlock, ToolParam, ToolUseBlock
from sqlalchemy.orm import Session

from app.agent.prompts import NEW_INPUT_SYSTEM_PROMPT, THREAD_FOLLOWUP_SYSTEM_PROMPT
from app.agent.tools import NEW_INPUT_TOOLS, THREAD_FOLLOWUP_TOOLS
from app.config import get_settings
from app.embeddings import embed
from app.notifications import notify_task_created
from app.schemas.task import TaskCreate
from app.storage import raw_inputs, tasks
from app.storage.raw_inputs import SimilarInput

log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 2
MAX_TOKENS = 1024
TEMPERATURE = 0.4
SIMILAR_K = 8


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
        prior = raw_inputs.find_by_thread(session, raw.source, thread_id)
        if prior is not None and prior.task_id is not None:
            task = tasks.get(session, prior.task_id)
            if task is not None:
                log.info(
                    "branch=thread_followup · raw=%s task=%s (prior_raw=%s)",
                    raw_input_id, task.id, prior.id,
                )
                return await _run_thread_followup(session, raw, task)

    # --- 2. Embed once -------------------------------------------------------
    query_text = _candidate_query_text(raw.content, meta)
    query_embedding: list[float] | None = raw.embedding
    if query_embedding is None and query_text:
        log.debug("embed · raw=%s len=%d", raw_input_id, len(query_text))
        query_embedding = await embed(query_text)
        if query_embedding is not None:
            raw_inputs.set_embedding(session, raw_input_id, query_embedding)
            session.commit()
    if query_embedding is None:
        log.info("embed · raw=%s NO embedding (no api key or empty text)", raw_input_id)

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
    return await _run_new_input_agent(
        session, raw, open_hits, closed_hits, not_task_hits, query_embedding
    )


# --- manual promotion: agent-fills-missing-fields ----------------------------


async def extract_task_fields(raw) -> dict[str, Any]:
    """Ask the LLM to extract task fields from a raw input.

    Used by the manual promotion endpoint when the user hasn't supplied
    title/estimation/due_date. Only the `create_task` tool is offered — the
    user has already decided this is a task, so the agent's job is purely
    extraction (no dedup, no candidates, no `mark_not_task`).
    """
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_msg = _build_extract_message(raw)
    create_tool = next(t for t in NEW_INPUT_TOOLS if t["name"] == "create_task")

    log.info("llm call · branch=extract_fields raw=%s", raw.id)
    resp = await client.messages.create(
        model=settings.claude_model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=_cached_system(EXTRACT_FIELDS_SYSTEM_PROMPT),
        tools=_cached_tools([create_tool]),
        tool_choice={"type": "tool", "name": "create_task"},
        messages=[{"role": "user", "content": user_msg}],
    )
    log.debug(
        "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
        raw.id, resp.stop_reason,
        getattr(resp.usage, "input_tokens", "?"),
        getattr(resp.usage, "output_tokens", "?"),
    )

    tool_uses = [b for b in resp.content if isinstance(b, ToolUseBlock)]
    if not tool_uses:
        raise RuntimeError("agent did not call create_task during field extraction")
    payload = dict(tool_uses[0].input or {})
    if "due_date" in payload:
        payload["due_date"] = _parse_iso(payload["due_date"])
    return payload


def _build_extract_message(raw) -> str:
    meta = raw.source_metadata or {}
    lines = [f"Source: {raw.source}"]
    for key in ("from", "to", "subject", "date", "thread_id", "account"):
        val = meta.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")
    lines.append("")
    lines.append("Body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)


EXTRACT_FIELDS_SYSTEM_PROMPT = """\
You are extracting structured task fields from a raw input the user has
explicitly chosen to promote to a task. Do NOT second-guess the decision —
your only job is to populate `create_task` accurately:

- title: short, imperative.
- estimation: minutes; required, best-guess.
- due_date: ISO 8601; required — use the explicit deadline if stated,
  otherwise a reasonable best-guess based on urgency.
- description, location, link: include when supported by the input.

Call `create_task` exactly once. Do not narrate.
"""


# --- thread-followup agent ----------------------------------------------------


async def _run_thread_followup(session: Session, raw, task) -> dict:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    user_msg = _build_thread_user_message(raw, task)
    trace: dict[str, Any] = {"outcome": None, "branch": "thread_followup", "task_id": str(task.id)}
    final_status = "open"
    final_task_id = task.id

    messages: list[MessageParam] = [{"role": "user", "content": user_msg}]
    log.info("llm call · branch=thread_followup raw=%s task=%s", raw.id, task.id)
    resp = await client.messages.create(
        model=settings.claude_model,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        system=_cached_system(THREAD_FOLLOWUP_SYSTEM_PROMPT),
        tools=_cached_tools(THREAD_FOLLOWUP_TOOLS),
        messages=messages,
    )
    log.debug(
        "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
        raw.id, resp.stop_reason,
        getattr(resp.usage, "input_tokens", "?"),
        getattr(resp.usage, "output_tokens", "?"),
    )
    trace["blocks"] = [_block_summary(b) for b in resp.content]

    tool_uses = [b for b in resp.content if isinstance(b, ToolUseBlock)]
    if not tool_uses:
        trace["outcome"] = "no_tool_call"
    else:
        tu = tool_uses[0]
        trace["tool"] = {"name": tu.name, "input": tu.input}
        if tu.name == "update_task":
            patch = {k: v for k, v in (tu.input or {}).items() if v is not None}
            if "due_date" in patch:
                patch["due_date"] = _parse_iso(patch["due_date"])
            tasks.update(session, task.id, **patch)
            trace["outcome"] = "updated"
            final_status = "open"
        elif tu.name == "close_task":
            trace["outcome"] = "closed"
            trace["reason"] = (tu.input or {}).get("reason")
            final_status = "closed"
        elif tu.name == "no_change":
            trace["outcome"] = "no_change"
            trace["reason"] = (tu.input or {}).get("reason")
            final_status = "open"
        else:
            trace["outcome"] = f"unknown_tool:{tu.name}"

    raw_inputs.finalize(
        session, raw.id, status=final_status, task_id=final_task_id, agent_trace=trace
    )
    session.commit()
    return trace


def _build_thread_user_message(raw, task) -> str:
    meta = raw.source_metadata or {}
    lines = [f"Source: {raw.source}"]
    for key in ("from", "to", "subject", "date", "thread_id"):
        val = meta.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")

    lines.append("")
    lines.append("Current task:")
    lines.append(f"  id: {task.id}")
    lines.append(f"  title: {task.title}")
    if task.description:
        lines.append(f"  description: {task.description}")
    if task.due_date:
        lines.append(f"  due_date: {task.due_date.isoformat()}")
    if task.estimation is not None:
        lines.append(f"  estimation: {task.estimation} min")
    if task.location:
        lines.append(f"  location: {task.location}")
    if task.link:
        lines.append(f"  link: {task.link}")

    lines.append("")
    lines.append("Follow-up body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)


# --- new-input agent ----------------------------------------------------------


async def _run_new_input_agent(
    session: Session,
    raw,
    open_hits: list[SimilarInput],
    closed_hits: list[SimilarInput],
    not_task_hits: list[SimilarInput],
    query_embedding: list[float] | None,
) -> dict:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Open-task candidates: deduplicate by task_id; load fields for the prompt.
    open_task_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for hit in open_hits:
        if hit.task_id and hit.task_id not in seen:
            seen.add(hit.task_id)
            open_task_ids.append(hit.task_id)

    open_tasks = [tasks.get(session, tid) for tid in open_task_ids]
    open_tasks = [t for t in open_tasks if t is not None]

    user_msg = _build_new_input_message(
        raw, open_tasks, not_task_hits, closed_hits
    )

    trace: dict[str, Any] = {
        "outcome": None,
        "branch": "new_input",
        "embedded_query": query_embedding is not None,
        "candidates_open": len(open_tasks),
        "precedents_not_task": len(not_task_hits),
        "precedents_closed": len(closed_hits),
        "iterations": [],
    }
    final_status = "not_task"
    final_task_id: uuid.UUID | None = None

    messages: list[MessageParam] = [{"role": "user", "content": user_msg}]
    log.info(
        "llm call · branch=new_input raw=%s candidates=%d not_task_precedents=%d closed_precedents=%d",
        raw.id, len(open_tasks), len(not_task_hits), len(closed_hits),
    )

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = await client.messages.create(
            model=settings.claude_model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=_cached_system(NEW_INPUT_SYSTEM_PROMPT),
            tools=_cached_tools(NEW_INPUT_TOOLS),
            messages=messages,
        )
        log.debug(
            "llm response · raw=%s stop_reason=%s input_tokens=%s output_tokens=%s",
            raw.id, resp.stop_reason,
            getattr(resp.usage, "input_tokens", "?"),
            getattr(resp.usage, "output_tokens", "?"),
        )
        iter_log = {
            "stop_reason": resp.stop_reason,
            "blocks": [_block_summary(b) for b in resp.content],
        }
        trace["iterations"].append(iter_log)

        tool_uses = [b for b in resp.content if isinstance(b, ToolUseBlock)]
        if not tool_uses:
            trace["outcome"] = trace["outcome"] or "no_tool_call"
            break

        tu = tool_uses[0]
        iter_log["tool"] = {"name": tu.name, "input": tu.input}

        if tu.name == "create_task":
            payload = dict(tu.input or {})
            if "due_date" in payload:
                payload["due_date"] = _parse_iso(payload["due_date"])
            task = tasks.create(
                session,
                TaskCreate(
                    title=payload["title"],
                    description=payload.get("description"),
                    estimation=payload.get("estimation"),
                    due_date=payload.get("due_date"),
                    location=payload.get("location"),
                    link=payload.get("link"),
                ),
            )
            trace["outcome"] = "task_created"
            trace["task_id"] = str(task.id)
            final_status = "open"
            final_task_id = task.id
            await notify_task_created(task, raw)
            break
        if tu.name == "mark_duplicate":
            existing_id = uuid.UUID((tu.input or {})["existing_task_id"])
            trace["outcome"] = "duplicate"
            trace["existing_task_id"] = str(existing_id)
            trace["reason"] = (tu.input or {}).get("reason")
            final_status = "duplicate"
            final_task_id = existing_id
            break
        if tu.name == "mark_not_task":
            trace["outcome"] = "not_task"
            trace["reason"] = (tu.input or {}).get("reason")
            final_status = "not_task"
            break

        # Unknown tool — surface and stop.
        trace["outcome"] = f"unknown_tool:{tu.name}"
        break
    else:
        trace["outcome"] = trace["outcome"] or "max_iterations"

    raw_inputs.finalize(
        session,
        raw.id,
        status=final_status,
        task_id=final_task_id,
        agent_trace=trace,
    )
    session.commit()
    return trace


def _build_new_input_message(
    raw, open_tasks, not_task_hits: list[SimilarInput], closed_hits: list[SimilarInput]
) -> str:
    meta = raw.source_metadata or {}
    lines = [f"Source: {raw.source}"]
    for key in ("from", "to", "subject", "date", "thread_id", "account"):
        val = meta.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")

    if open_tasks:
        lines.append("")
        lines.append("Candidate existing tasks (use `mark_duplicate` if one matches):")
        for t in open_tasks:
            desc = (t.description or "").strip().replace("\n", " ")
            if len(desc) > 160:
                desc = desc[:160] + "…"
            lines.append(f"- {t.id} | {t.title}" + (f" — {desc}" if desc else ""))

    precedent_lines: list[str] = []
    for p in not_task_hits[:3]:
        reason = (p.agent_trace or {}).get("reason") or ""
        precedent_lines.append(
            f"- sim={p.similarity:.2f} | NOT_TASK | from {p.sender or '?'} | "
            f"{p.subject or '(no subject)'} | {reason[:120]}"
        )
    for p in closed_hits[:3]:
        precedent_lines.append(
            f"- sim={p.similarity:.2f} | CLOSED_TASK_PRECEDENT (task {p.task_id}) | "
            f"from {p.sender or '?'} | {p.subject or '(no subject)'}"
        )
    if precedent_lines:
        lines.append("")
        lines.append("Past similar inputs (precedents — strong signal):")
        lines.extend(precedent_lines)

    lines.append("")
    lines.append("Body:")
    lines.append((raw.content or "").strip() or "(empty)")
    return "\n".join(lines)


# --- helpers ------------------------------------------------------------------


def _candidate_query_text(content: str, metadata: dict) -> str:
    parts: list[str] = []
    subject = metadata.get("subject")
    if subject:
        parts.append(subject)
    body = (content or "").strip()
    if body:
        parts.append(body[:1500])
    return "\n".join(parts).strip()


def _parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    # Accept trailing Z for UTC, which fromisoformat doesn't.
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _cached_system(prompt: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


def _cached_tools(tools: list[dict]) -> list[ToolParam]:
    out = [dict(t) for t in tools]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return cast(list[ToolParam], out)


def _block_summary(block) -> dict:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": (block.text or "")[:500]}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "name": block.name, "input": block.input}
    return {"type": getattr(block, "type", "unknown")}
