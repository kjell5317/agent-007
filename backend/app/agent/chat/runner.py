"""Chat / ask-mode agent (docs/search-plan.md stage 3).

Retrieval-first and fast: every user turn injects the top hybrid hits (local +
Drive) into context up front — the model answers from them without a tool
round-trip. The same hits are exposed via citation tags, and `search` /
`find_calendar_events` remain as multi-query drill-down tools. A full agent
loop lets the model call any action tool (create/update/close tasks, events,
notes) then answer; unlike the input flows there is no single terminal tool.

The runner is transport-agnostic: it pushes structured events (`citations`,
`token`, `tool_call`, `done`) to an `emit` callback that the SSE endpoint drains.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.agent.helpers.dispatch import apply_task_action
from app.agent.helpers.llm import (
    LLMMessage,
    ToolCall,
    assistant_message,
    stream_chat,
    tool_result_message,
    user_message,
)
from app.agent.helpers.text import normalize_agent_due_date, now_iso
from app.agent.prompts import CHAT_SYSTEM_PROMPT
from app.agent.tools import (
    CHAT_TOOLS,
    NOTION_CHAT_TOOLS,
    run_create_event,
    run_delete_event,
    run_update_event,
)
from app.config import get_settings
from app.db.clients import notes as notes_store
from app.db.clients import tasks as tasks_store
from app.db.schemas.search import SearchHit
from app.db.schemas.task import TaskCreate
from app.services import notion_mcp
from app.services.input.embedding import embed
from app.services.plan import schedule_task
from app.services.search.drive import get_drive_file
from app.services.search.filters import Filters
from app.services.search.retrieve import list_tasks, retrieve

log = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

# Citation tag prefixes by hit type. "D" = document (kotx/GitHub issues, etc.),
# kept distinct from "E" = calendar event so a document isn't read as an event;
# "G" = Google Drive file.
_TAG_PREFIX = {
    "task": "T",
    "input": "I",
    "note": "N",
    "document": "D",
    "calendar": "E",
    "drive": "G",
}


@dataclass
class ChatTurn:
    role: str  # "user" | "assistant"
    content: str


class Citations:
    """Assigns stable citation tags ([T1], [I2], …) to hits and dedupes across
    the turn so the initial context and any `search` tool results share one
    numbering the model can cite."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, SearchHit]] = []
        self._counts: dict[str, int] = {}
        self._seen: set[tuple[str, str]] = set()

    def add(self, hits: list[SearchHit]) -> list[tuple[str, SearchHit]]:
        new: list[tuple[str, SearchHit]] = []
        for h in hits:
            key = (h.type, h.id)
            if key in self._seen:
                continue
            self._seen.add(key)
            prefix = _TAG_PREFIX.get(h.type, "R")
            self._counts[prefix] = self._counts.get(prefix, 0) + 1
            entry = (f"{prefix}{self._counts[prefix]}", h)
            self._entries.append(entry)
            new.append(entry)
        return new


def _sse_item(tag: str, h: SearchHit) -> dict[str, Any]:
    return {"tag": tag, **h.model_dump(mode="json")}


def _context_line(tag: str, h: SearchHit) -> str:
    head = f"[{tag}] {h.type}"
    if h.status and h.status != h.type and h.status != "drive":
        head += f"/{h.status}"
    meta: list[str] = []
    if h.sender:
        meta.append(f"from {h.sender}")
    if h.source and h.source != h.type:
        meta.append(h.source)
    if h.ts:
        meta.append(h.ts.date().isoformat())
    line = f"{head}: {h.title or '(untitled)'}"
    if meta:
        line += " (" + ", ".join(meta) + ")"
    if h.snippet:
        line += f" — {h.snippet[:200]}"
    if h.task_id:
        line += f" [task_id={h.task_id}]"
    # Calendar / Drive hits carry the id the action tools need (event_id →
    # update_event, file_id → get_drive_file). Surfacing it stops the model
    # guessing the tag or title as the id.
    elif h.type == "document" and h.source == "calendar":
        line += f" [event_id={h.id}]"
    elif h.type == "drive":
        line += f" [file_id={h.id}]"
    return line


def _context_block(tz: str, entries: list[tuple[str, SearchHit]]) -> str:
    lines = [f"Current time: {now_iso(tz)}", ""]
    if entries:
        lines.append("Retrieved context (cite items with their bracketed tag):")
        lines.extend(_context_line(tag, h) for tag, h in entries)
    else:
        lines.append("Retrieved context: no matching items were found.")
    return "\n".join(lines)


async def run_chat(session: Session, turns: list[ChatTurn], *, emit: Emit) -> None:
    settings = get_settings()
    history = turns[-settings.search_chat_history_messages :]
    last_user_idx = _last_user_index(history)
    query = history[last_user_idx].content if last_user_idx is not None else ""

    cites = Citations()
    # Same retrieval path as the `search` tool (local + calendar + Drive), so the
    # up-front context and a manual re-query behave identically. `retrieve` is
    # resilient — a failing backend degrades to [] rather than sinking the answer.
    entries = cites.add(await retrieve(session, query))
    await emit("citations", {"items": [_sse_item(tag, h) for tag, h in entries]})

    context = _context_block(settings.user_timezone, entries)
    messages = _build_messages(history, last_user_idx, context)

    # Notion's read-only tools only appear when a workspace is connected, so the
    # model never sees a tool that would just fail with "not connected".
    tools = CHAT_TOOLS + (NOTION_CHAT_TOOLS if notion_mcp.is_connected(session) else [])

    async def on_delta(text: str) -> None:
        await emit("token", {"text": text})

    for _ in range(settings.search_chat_max_iterations):
        resp = await stream_chat(
            messages,
            settings,
            system_prompt=CHAT_SYSTEM_PROMPT,
            tools=tools,
            on_delta=on_delta,
        )
        if not resp.tool_calls:
            break
        messages.append(assistant_message(resp))
        for tc in resp.tool_calls:
            result_text, trace = await _dispatch(session, cites, tc, settings, emit)
            await emit("tool_call", trace)
            messages.append(tool_result_message(tc, result_text))
    else:
        # Iterations exhausted while the model was still calling tools — surface
        # that, then force a final tool-less answer so the user always gets a
        # response instead of a bubble that just stops after the last tool call.
        await emit(
            "tool_call",
            _trace(
                "tool_limit",
                purpose="Reached tool limit",
                summary=f"Stopped after {settings.search_chat_max_iterations} tool steps.",
                status="failed",
            ),
        )
        await stream_chat(
            messages,
            settings,
            system_prompt=CHAT_SYSTEM_PROMPT,
            tools=[],
            on_delta=on_delta,
        )

    await emit("done", {})


def _last_user_index(history: list[ChatTurn]) -> int | None:
    for i in range(len(history) - 1, -1, -1):
        if history[i].role == "user":
            return i
    return None


def _build_messages(
    history: list[ChatTurn], last_user_idx: int | None, context: str
) -> list[LLMMessage]:
    messages: list[LLMMessage] = []
    for i, turn in enumerate(history):
        if turn.role == "user":
            text = turn.content
            if i == last_user_idx:
                text = f"{turn.content}\n\n{context}"
            messages.append(user_message(text))
        else:
            messages.append(LLMMessage(role="assistant", text=turn.content))
    if not messages:
        messages.append(user_message(context))
    return messages


def _trace(
    name: str,
    *,
    purpose: str,
    summary: str,
    status: str = "success",
    changed_state: bool = False,
    artifact_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "purpose": purpose,
        "result_summary": summary[:500],
        "changed_state": changed_state,
        "artifact_refs": artifact_refs or [],
    }


async def _dispatch(
    session: Session, cites: Citations, tc: ToolCall, settings, emit: Emit
) -> tuple[str, dict[str, Any]]:
    """Run one tool call. Returns (text_for_llm, trace). Tool errors degrade to
    a failed trace + explanatory text rather than aborting the chat."""
    name = tc.name
    tin = tc.input or {}
    try:
        if name == "search":
            return await _search(session, cites, tin, emit)

        if name == "list_tasks":
            hits = list_tasks(
                session,
                status=str(tin.get("status") or "open"),
                due_after=(str(tin["due_after"]) if tin.get("due_after") else None),
                due_before=(str(tin["due_before"]) if tin.get("due_before") else None),
                label=(str(tin["label"]) if tin.get("label") else None),
            )
            new = cites.add(hits)
            if new:
                await emit("citations", {"items": [_sse_item(tag, h) for tag, h in new]})
            body = "\n".join(_context_line(tag, h) for tag, h in new) or "no matching tasks"
            return (
                f"tasks:\n{body}",
                _trace(name, purpose="list tasks", summary=f"{len(new)} tasks"),
            )

        if name == "get_drive_file":
            file_id = str(tin.get("file_id") or "")
            out = await get_drive_file(
                session, file_id, max_chars=settings.search_drive_file_max_chars
            )
            # The output leads with the file's name in quotes; show it on the chip.
            name_match = re.match(r"^'([^']+)'", out)
            label = name_match.group(1) if name_match else (file_id or "drive file")
            return out, _trace(name, purpose=f"read {label}"[:80], summary=out)

        if name == "create_task":
            return await _create_task(session, tin)

        if name == "update_task":
            return await _update_task(session, tin)

        if name == "create_event":
            out, event_id = await run_create_event(
                session,
                summary=str(tin.get("summary") or ""),
                start=str(tin.get("start") or ""),
                end=str(tin.get("end")) if tin.get("end") else None,
                description=str(tin.get("description")) if tin.get("description") else None,
                location=str(tin.get("location")) if tin.get("location") else None,
            )
            return out, _trace(
                name,
                purpose=f"create event {tin.get('summary') or ''}"[:80],
                summary=out,
                status="success" if event_id else "failed",
                changed_state=bool(event_id),
                artifact_refs=[f"event:{event_id}"] if event_id else [],
            )

        if name == "update_event":
            # Consolidated edit/delete, mirroring update_task's lifecycle field.
            deleting = bool(tin.get("delete"))
            if deleting:
                out, event_id = await run_delete_event(
                    session, event_id=str(tin.get("event_id") or "")
                )
            else:
                out, event_id = await run_update_event(
                    session,
                    event_id=str(tin.get("event_id") or ""),
                    summary=str(tin.get("summary")) if tin.get("summary") is not None else None,
                    start=str(tin.get("start")) if tin.get("start") else None,
                    end=str(tin.get("end")) if tin.get("end") else None,
                    description=(
                        str(tin.get("description")) if tin.get("description") is not None else None
                    ),
                    location=str(tin.get("location")) if tin.get("location") is not None else None,
                )
            return out, _trace(
                name,
                purpose="delete event" if deleting else "update event",
                summary=out,
                status="success" if event_id else "failed",
                changed_state=bool(event_id),
                artifact_refs=[f"event:{event_id}"] if event_id else [],
            )

        if name == "create_note":
            return await _create_note(session, tin)

        if name == "notion_search":
            query = str(tin.get("query") or "").strip()
            out = await notion_mcp.notion_search(session, query)
            return out, _trace(name, purpose=f"notion search: {query}"[:80], summary=out)

        if name == "notion_fetch":
            ref = str(tin.get("id") or "").strip()
            out = await notion_mcp.notion_fetch(session, ref)
            return out, _trace(name, purpose=f"notion fetch: {ref}"[:80], summary=out)

        return f"unknown tool: {name}", _trace(name, purpose=name, summary="unknown tool", status="failed")
    except Exception as exc:  # noqa: BLE001 — one bad tool must not kill the chat
        log.exception("chat tool %s failed", name)
        return (
            f"{name} failed: {exc}",
            _trace(name, purpose=name, summary=str(exc), status="failed"),
        )


async def _search(
    session: Session, cites: Citations, tin: dict[str, Any], emit: Emit
) -> tuple[str, dict[str, Any]]:
    """The multi-query retrieval tool — the same `retrieve` path as the up-front
    context. `source` routes the fan-out (`drive`/`calendar` = that API only,
    another source narrows the local search, none = everything); the metadata
    filters apply across every backend."""
    q = str(tin.get("query") or "").strip()
    filters = Filters(
        source=(str(tin["source"]).lower() if tin.get("source") else None),
        label=(str(tin["label"]) if tin.get("label") else None),
        status=(str(tin["status"]) if tin.get("status") else None),
        before=(str(tin["before"]) if tin.get("before") else None),
        after=(str(tin["after"]) if tin.get("after") else None),
    )
    hits = await retrieve(session, q, filters=filters)
    new = cites.add(hits)
    if new:
        await emit("citations", {"items": [_sse_item(tag, h) for tag, h in new]})
    body = "\n".join(_context_line(tag, h) for tag, h in new) or "no new matches"
    return (
        f"search results for '{q}':\n{body}",
        _trace("search", purpose=f"search: {q}"[:80], summary=f"{len(new)} new hits"),
    )


async def _create_task(session: Session, tin: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    due = normalize_agent_due_date(tin.get("due_date"))
    task = tasks_store.create(
        session,
        TaskCreate(
            title=str(tin["title"]),
            description=str(tin.get("description")) if tin.get("description") else None,
            estimation=tin.get("estimation") if tin.get("estimation") else None,
            due_date=due,
            location=str(tin.get("location")) if tin.get("location") else None,
            link=str(tin.get("link")) if tin.get("link") else None,
            label=str(tin.get("label")) if tin.get("label") else None,
        ),
    )
    await schedule_task(session, task)
    session.commit()
    return (
        f"Created task '{task.title}' (id {task.id}).",
        _trace(
            "create_task",
            purpose=f"create task {task.title}"[:80],
            summary=f"created task {task.id}",
            changed_state=True,
            artifact_refs=[f"task:{task.id}"],
        ),
    )


async def _update_task(session: Session, tin: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw_id = tin.get("task_id")
    try:
        task_id = uuid.UUID(str(raw_id))
    except (ValueError, TypeError):
        return "update_task: `task_id` is not a valid id.", _trace(
            "update_task", purpose="update task", summary="invalid task_id", status="failed"
        )
    task = tasks_store.get(session, task_id)
    if task is None:
        return "update_task: no task with that id.", _trace(
            "update_task", purpose="update task", summary="task not found", status="failed"
        )
    patch = {k: v for k, v in tin.items() if k != "task_id"}
    frag = await apply_task_action(session, task, "update_task", patch)
    session.commit()
    outcome = str(frag.get("outcome") or "updated")
    return (
        f"{outcome} task {task_id}.",
        _trace(
            "update_task",
            purpose=f"{outcome} task {task.title}"[:80],
            summary=f"{outcome} {task_id}",
            changed_state=outcome != "no_change",
            artifact_refs=[f"task:{task_id}"],
        ),
    )


async def _create_note(session: Session, tin: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    content = str(tin.get("content") or "").strip()
    if not content:
        return "create_note: `content` is required.", _trace(
            "create_note", purpose="save note", summary="empty content", status="failed"
        )
    vec = await embed(content)
    notes_store.create(session, content=content, source_raw_input_id=None, embedding=vec)
    session.commit()
    return (
        "Saved note to long-term memory.",
        _trace(
            "create_note",
            purpose="save note",
            summary=content[:120],
            changed_state=True,
        ),
    )
