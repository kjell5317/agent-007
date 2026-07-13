"""Chat / ask-mode agent (docs/search-plan.md stage 3).

Retrieval-first and fast: every user turn injects the top local hits (tasks +
notes) into context up front — the model answers from them without a tool
round-trip. Those hits are exposed via citation tags. Every other source is a
dedicated drill-down tool (`messages_search`, `calendar_search`, `drive_search`,
`contacts_search`, `tasks_search`, `search_notes`, plus GitHub/Notion), so the
model routes to the one source a question needs rather than fanning out blindly.
A full agent loop lets it call any action tool (create/update/close tasks,
events, notes) then answer; unlike the input flows there is no terminal tool.

Every search path returns `SearchHit`s that render into one uniform context
record (`type · sim · date · id · meta — content`), so results read identically
whatever source they came from.

The runner is transport-agnostic: it pushes structured events (`citations`,
`response_mode`, `token`, `tool_call`, `done`) to an `emit` callback that the
SSE endpoint drains.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

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
    GITHUB_CHAT_TOOLS,
    NOTION_CHAT_TOOLS,
    run_create_event,
    run_delete_event,
    run_update_event,
)
from app import observability as obs
from app.config import get_settings
from app.db.clients import notes as notes_store
from app.db.clients import tasks as tasks_store
from app.db.schemas.search import SearchHit
from app.db.schemas.task import TaskCreate
from app.services import github, notion_mcp
from app.services.input.embedding import embed
from app.services.plan import schedule_task
from app.services.search.contacts import search_contacts
from app.services.search.drive import get_drive_file, search_drive
from app.services.search.retrieve import (
    find_tasks,
    retrieve,
    search_calendar,
    search_messages,
    search_notes,
)

log = logging.getLogger(__name__)

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
ResponseMode = Literal["sources", "answer"]

# Citation tag prefixes by hit type. "E" = calendar event (a document with
# source=calendar); "G" = Google Drive file; "C" = contact. "D" is kept for any
# non-calendar document.
_TAG_PREFIX = {
    "task": "T",
    "input": "I",
    "note": "N",
    "document": "D",
    "calendar": "E",
    "drive": "G",
    "contact": "C",
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
            prefix = _tag_prefix(h)
            self._counts[prefix] = self._counts.get(prefix, 0) + 1
            entry = (f"{prefix}{self._counts[prefix]}", h)
            self._entries.append(entry)
            new.append(entry)
        return new


def _tag_prefix(h: SearchHit) -> str:
    # Calendar events are documents with source=calendar; tag them "E" (event),
    # distinct from a plain "D" document, matching the prompt's citation legend.
    if h.type == "document" and h.source == "calendar":
        return "E"
    return _TAG_PREFIX.get(h.type, "R")


def _sse_item(tag: str, h: SearchHit) -> dict[str, Any]:
    return {"tag": tag, **h.model_dump(mode="json")}


def _context_line(tag: str, h: SearchHit) -> str:
    """One hit as the uniform context record every source shares:
    `[tag] type · sim · date · id=<source_id> · <meta> — title — content`.

    `id=` is always the source_id a get/act tool consumes for THIS item
    (task_id, event_id, file_id, note id, message id, contact resourceName);
    a linked task shows separately as `task=<id>` for the task widget."""
    meta = h.meta or {}
    seg: list[str] = [h.type]
    if "similarity" in meta:
        seg.append(f"sim={meta['similarity']:.2f}")
    if h.ts:
        seg.append(h.ts.date().isoformat())
    seg.append(f"id={h.id}")
    # Origin + lifecycle, when they add information beyond the type itself.
    if h.source and h.source not in (h.type, "note", "drive", "contact", "contacts"):
        seg.append(h.source)
    if h.status and h.status not in (h.type, "note", "drive", "contact", "event"):
        seg.append(h.status)
    if h.sender:
        seg.append(f"from {h.sender}")
    # Source-specific extras.
    if meta.get("start"):
        seg.append(_clock(meta["start"]))
    if meta.get("location"):
        seg.append(f"@ {meta['location']}")
    if meta.get("mime"):
        seg.append(str(meta["mime"]))
    for field in ("emails", "phones"):
        if meta.get(field):
            seg.append(", ".join(meta[field]))
    if h.task_id and h.task_id != h.id:
        seg.append(f"task={h.task_id}")

    line = f"[{tag}] " + " · ".join(seg) + f" — {h.title or '(untitled)'}"
    # Append the snippet as content unless it just repeats something already in
    # the segments (drive mime label, calendar location, contact details).
    if (
        h.snippet
        and h.snippet != h.title
        and h.type not in ("drive", "contact")
        and h.snippet != meta.get("location")
    ):
        line += f" — {h.snippet[:200]}"
    return line


def _clock(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return str(iso)


_QUESTION_PREFIX_RE = re.compile(
    r"^(what|what's|whats|when|where|who|whose|why|how|which|is|are|am|was|were|"
    r"do|does|did|can|could|should|would|will|has|have|had|any)\b",
    re.I,
)
_COMMAND_PREFIX_RE = re.compile(
    r"^(create|add|make|update|edit|change|close|complete|finish|reopen|delete|remove|"
    r"reschedule|schedule|move|set|save|remember|note|list|show|tell|give|find|search|"
    r"look up|open|read|summarize|summarise|draft|send)\b",
    re.I,
)
_AGENDA_RE = re.compile(
    r"\b(todo|todos|task|tasks|due|overdue|agenda|calendar|schedule)\b.*\b(today|tomorrow|"
    r"week|month|morning|afternoon|evening)\b|\b(today|tomorrow|week|month|morning|"
    r"afternoon|evening)\b.*\b(todo|todos|task|tasks|due|overdue|agenda|calendar|schedule)\b",
    re.I,
)


def classify_response_mode(text: str) -> ResponseMode:
    """Classify a chat turn before prompting so source discovery is deterministic."""
    q = " ".join(text.strip().split())
    if not q:
        return "answer"
    if "?" in q or _QUESTION_PREFIX_RE.search(q):
        return "answer"
    if _COMMAND_PREFIX_RE.search(q):
        return "answer"
    if _AGENDA_RE.search(q):
        return "answer"
    # Terse keyword searches, tags, and comma-separated phrases should surface
    # related source cards instead of trying to synthesize an agenda-style answer.
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_@./#:-]*", q)
    if len(words) <= 4:
        return "sources"
    if "," in q and len(words) <= 8:
        return "sources"
    return "answer"


def _mode_instruction(mode: ResponseMode) -> str:
    if mode == "sources":
        return (
            "Response mode: sources\n"
            "The latest user input is a keyword-style source discovery query. "
            "Give a one- or two-sentence summary of the strongest signal from the "
            "retrieved context. The UI shows a related-source card for EACH item "
            "you cite, in the order you cite it, and nothing else — so you choose "
            "which sources surface and their ranking. Cite the relevant items "
            "inline, most relevant first; cite nothing if none are relevant. Do "
            "not write your own document/source list."
        )
    return (
        "Response mode: answer\n"
        "The latest user input is a question or command. Answer or act directly, "
        "concisely, with inline citations for facts. Do not offer a related-source "
        "list unless the user explicitly asks for sources."
    )


def _context_block(
    tz: str, entries: list[tuple[str, SearchHit]], mode: ResponseMode
) -> str:
    lines = [f"Current time: {now_iso(tz)}", _mode_instruction(mode), ""]
    if entries:
        lines.append("Retrieved context (cite items with their bracketed tag):")
        lines.extend(_context_line(tag, h) for tag, h in entries)
    else:
        lines.append("Retrieved context: no matching items were found.")
    return "\n".join(lines)


async def run_chat(
    session: Session, turns: list[ChatTurn], *, emit: Emit, session_id: str | None = None
) -> None:
    settings = get_settings()
    history = turns[-settings.search_chat_history_messages :]
    last_user_idx = _last_user_index(history)
    query = history[last_user_idx].content if last_user_idx is not None else ""
    response_mode = classify_response_mode(query)

    # Root span groups every LLM turn + tool call of this answer into one named,
    # session-scoped trace. `session_id` (the conversation id) lets the Langfuse
    # Sessions view stitch multi-turn conversations back together.
    answer_parts: list[str] = []
    with obs.root_span("chat-response", session_id=session_id, tags=["chat"]) as span:
        obs.set_trace_io(input=query)

        cites = Citations()
        # Fast local pre-injection: the top tasks + notes for this message, no
        # external calls. Everything else is a per-source tool the model calls on
        # demand. `retrieve` degrades to [] on an embed failure rather than sinking
        # the answer; a repeated tool search is harmless (citation dedup drops it).
        entries = cites.add(await retrieve(session, query))
        await emit("citations", {"items": [_sse_item(tag, h) for tag, h in entries]})
        await emit("response_mode", {"response_mode": response_mode})

        context = _context_block(settings.user_timezone, entries, response_mode)
        messages = _build_messages(history, last_user_idx, context)

        # Optional integrations expose their read-only tools only when connected, so
        # the model never sees a tool that would just fail with "not connected".
        tools = list(CHAT_TOOLS)
        if notion_mcp.is_connected(session):
            tools += NOTION_CHAT_TOOLS
        if github.is_connected():
            tools += GITHUB_CHAT_TOOLS

        async def on_delta(text: str) -> None:
            answer_parts.append(text)
            await emit("token", {"text": text})

        for _ in range(settings.search_chat_max_iterations):
            resp = await stream_chat(
                messages,
                settings,
                system_prompt=CHAT_SYSTEM_PROMPT,
                tools=tools,
                on_delta=on_delta,
                name="chat-turn",
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
                name="chat-final",
            )

        answer = "".join(answer_parts)
        obs.set_trace_io(output=answer)
        span.update(output=answer)

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


async def _emit_search(
    cites: Citations, emit: Emit, hits: list[SearchHit], *, name: str, purpose: str
) -> tuple[str, dict[str, Any]]:
    """Register hits, stream their citations, and format the shared uniform
    record. One path for every per-source search tool, so results read
    identically whatever source they came from."""
    new = cites.add(hits)
    if new:
        await emit("citations", {"items": [_sse_item(tag, h) for tag, h in new]})
    body = "\n".join(_context_line(tag, h) for tag, h in new) or "no matches"
    return f"{purpose}:\n{body}", _trace(name, purpose=purpose[:80], summary=f"{len(new)} hits")


def _opt(tin: dict[str, Any], key: str) -> str | None:
    return str(tin[key]) if tin.get(key) else None


async def _dispatch(
    session: Session, cites: Citations, tc: ToolCall, settings, emit: Emit
) -> tuple[str, dict[str, Any]]:
    """Run one tool call. Returns (text_for_llm, trace). Tool errors degrade to
    a failed trace + explanatory text rather than aborting the chat."""
    name = tc.name
    tin = tc.input or {}
    q = str(tin.get("query") or "")
    try:
        if name == "tasks_search":
            hits = await find_tasks(
                session,
                query=_opt(tin, "query"),
                status=_opt(tin, "status"),
                label=_opt(tin, "label"),
                due_after=_opt(tin, "due_after"),
                due_before=_opt(tin, "due_before"),
            )
            purpose = f"tasks: {q}" if q else "list tasks"
            return await _emit_search(cites, emit, hits, name=name, purpose=purpose)

        if name == "search_notes":
            hits = await search_notes(session, q)
            return await _emit_search(cites, emit, hits, name=name, purpose=f"notes: {q}")

        if name == "messages_search":
            hits = await search_messages(
                session,
                q,
                source=(_opt(tin, "source") or "").lower() or None,
                before=_opt(tin, "before"),
                after=_opt(tin, "after"),
            )
            return await _emit_search(cites, emit, hits, name=name, purpose=f"messages: {q}")

        if name == "calendar_search":
            hits = await search_calendar(
                session,
                query=_opt(tin, "query"),
                time_min=_opt(tin, "time_min"),
                time_max=_opt(tin, "time_max"),
            )
            return await _emit_search(cites, emit, hits, name=name, purpose="calendar")

        if name == "drive_search":
            hits = await search_drive(
                session,
                q,
                k=settings.search_chat_drive_limit,
                timeout=settings.search_drive_timeout_seconds,
                after=_opt(tin, "after"),
                before=_opt(tin, "before"),
            )
            return await _emit_search(cites, emit, hits, name=name, purpose=f"drive: {q}")

        if name == "contacts_search":
            hits = await search_contacts(
                session,
                q,
                k=settings.search_chat_contacts_limit,
                timeout=settings.search_contacts_timeout_seconds,
            )
            return await _emit_search(cites, emit, hits, name=name, purpose=f"contacts: {q}")

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

        if name == "github_search":
            query = str(tin.get("query") or "").strip()
            out = await github.search_issues(query)
            return out, _trace(name, purpose=f"github search: {query}"[:80], summary=out)

        if name == "github_my_work":
            out = await github.my_work()
            return out, _trace(name, purpose="github my work", summary=out)

        return f"unknown tool: {name}", _trace(name, purpose=name, summary="unknown tool", status="failed")
    except Exception as exc:  # noqa: BLE001 — one bad tool must not kill the chat
        log.exception("chat tool %s failed", name)
        return (
            f"{name} failed: {exc}",
            _trace(name, purpose=name, summary=str(exc), status="failed"),
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
