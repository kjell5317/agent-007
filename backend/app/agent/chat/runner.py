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
`token`, `tool_call`, `done`) to an `emit` callback that the SSE endpoint drains.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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
from app.agent.prompts import chat_system_prompt
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

# Truncation width for the query echoed on a tool-call chip in the UI.
_CHIP_QUERY_MAX = 48

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


def _context_line(tag: str, h: SearchHit, zone: ZoneInfo) -> str:
    """One hit as the uniform context record every source shares:
    `[tag] type · sim · date · id=<source_id> · <meta> — title — content`.

    `id=` is the source_id a get/act tool consumes for THIS item (task_id,
    event_id, file_id, message id, contact resourceName); a linked task shows
    separately as `task=<id>` for the task widget. Notes omit `id=` — no tool
    takes a note id, so it would only burn tokens; a note acts through its
    `task=` link instead.

    Times are rendered in the user's `zone` — the underlying timestamps are
    UTC-aware, so an unconverted date/clock would read hours off (an 18:00
    Berlin event as "16:00"), which the model then repeats to the user."""
    meta = h.meta or {}
    seg: list[str] = [h.type]
    if "similarity" in meta:
        seg.append(f"sim={meta['similarity']:.2f}")
    if h.ts:
        ts = h.ts.astimezone(zone) if h.ts.tzinfo else h.ts
        seg.append(ts.date().isoformat())
    if h.type != "note":
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
        seg.append(_clock(meta["start"], zone))
    if meta.get("location"):
        seg.append(f"@ {meta['location']}")
    if meta.get("mime"):
        seg.append(str(meta["mime"]))
    for field in ("emails", "phones", "addresses"):
        if meta.get(field):
            seg.append(", ".join(meta[field]))
    if meta.get("org"):
        seg.append(str(meta["org"]))
    if meta.get("birthday"):
        seg.append(f"born {meta['birthday']}")
    if h.task_id and h.task_id != h.id:
        seg.append(f"task={h.task_id}")

    prefix = f"[{tag}] " + " · ".join(seg)
    title = h.title or "(untitled)"
    snippet = h.snippet
    # Collapse title + snippet to one body when either already contains the
    # other, so we never spend tokens on the same text twice. Notes are the
    # guaranteed case: title=content[:80] and snippet=content[:200] are both
    # prefixes of the same text, so we keep the fuller snippet and drop the
    # redundant short title. Drive/contact snippets and a snippet equal to the
    # location are already in the segments above, so they collapse to the title.
    if (
        not snippet
        or h.type in ("drive", "contact")
        or snippet == meta.get("location")
        or title.startswith(snippet)
    ):
        body = title
    elif snippet.startswith(title):
        body = snippet[:200]
    else:
        body = f"{title} — {snippet[:200]}"
    return f"{prefix} — {body}"


def _clock(iso: str, zone: ZoneInfo) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(zone)
    return dt.strftime("%H:%M")


def _chip_query(query: str | None) -> str:
    """Collapse and truncate a tool's query for the chip label so a long query
    never blows out the chip; every tool with a query echoes it this way."""
    q = " ".join((query or "").split())
    return q if len(q) <= _CHIP_QUERY_MAX else q[: _CHIP_QUERY_MAX - 1].rstrip() + "…"


def _purpose(verb: str, query: str | None, *, fallback: str | None = None) -> str:
    """`<verb>: <truncated query>` when a query is present, else `verb` (or an
    explicit `fallback`)."""
    q = _chip_query(query)
    return f"{verb}: {q}" if q else (fallback or verb)


def _context_block(tz: str, entries: list[tuple[str, SearchHit]]) -> str:
    zone = ZoneInfo(tz)
    lines = [f"Current time: {now_iso(tz)}", ""]
    if entries:
        lines.append("Retrieved context (cite items with their bracketed tag):")
        lines.extend(_context_line(tag, h, zone) for tag, h in entries)
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

        context = _context_block(settings.user_timezone, entries)
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

        system_prompt = chat_system_prompt()
        for _ in range(settings.search_chat_max_iterations):
            resp = await stream_chat(
                messages,
                settings,
                system_prompt=system_prompt,
                tools=tools,
                on_delta=on_delta,
                name="chat-turn",
            )
            if not resp.tool_calls:
                break
            messages.append(assistant_message(resp))
            for tc in resp.tool_calls:
                result_text, trace = await _dispatch(session, cites, tc, settings, emit)
                # Surface the raw call + full result so the UI can expand a chip
                # into params/result; result_summary stays the collapsed label.
                trace["params"] = tc.input or {}
                trace["result"] = result_text
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
                system_prompt=system_prompt,
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
        # params/result are overwritten with the real call in the dispatch loop;
        # these defaults keep the shape consistent for traces emitted directly.
        "params": {},
        "result": summary,
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
    zone = ZoneInfo(get_settings().user_timezone)
    body = "\n".join(_context_line(tag, h, zone) for tag, h in new) or "no matches"
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
            purpose = _purpose("tasks", q, fallback="list tasks")
            return await _emit_search(cites, emit, hits, name=name, purpose=purpose)

        if name == "search_notes":
            hits = await search_notes(session, q)
            return await _emit_search(cites, emit, hits, name=name, purpose=_purpose("notes", q))

        if name == "messages_search":
            hits = await search_messages(
                session,
                q,
                source=(_opt(tin, "source") or "").lower() or None,
                before=_opt(tin, "before"),
                after=_opt(tin, "after"),
            )
            return await _emit_search(cites, emit, hits, name=name, purpose=_purpose("messages", q))

        if name == "calendar_search":
            hits = await search_calendar(
                session,
                query=_opt(tin, "query"),
                time_min=_opt(tin, "time_min"),
                time_max=_opt(tin, "time_max"),
            )
            purpose = _purpose("calendar", _opt(tin, "query"), fallback="calendar")
            return await _emit_search(cites, emit, hits, name=name, purpose=purpose)

        if name == "drive_search":
            hits = await search_drive(
                session,
                q,
                k=settings.search_chat_drive_limit,
                timeout=settings.search_drive_timeout_seconds,
                after=_opt(tin, "after"),
                before=_opt(tin, "before"),
            )
            return await _emit_search(cites, emit, hits, name=name, purpose=_purpose("drive", q))

        if name == "contacts_search":
            hits = await search_contacts(
                session,
                q,
                k=settings.search_chat_contacts_limit,
                timeout=settings.search_contacts_timeout_seconds,
            )
            return await _emit_search(cites, emit, hits, name=name, purpose=_purpose("contacts", q))

        if name == "get_drive_file":
            file_id = str(tin.get("file_id") or "")
            out = await get_drive_file(
                session, file_id, max_chars=settings.search_drive_file_max_chars
            )
            # The output leads with the file's name in quotes; show it on the chip.
            name_match = re.match(r"^'([^']+)'", out)
            label = name_match.group(1) if name_match else (file_id or "drive file")
            return out, _trace(name, purpose=f"read {_chip_query(label)}", summary=out)

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
            return out, _trace(name, purpose=_purpose("notion", query), summary=out)

        if name == "notion_fetch":
            ref = str(tin.get("id") or "").strip()
            out = await notion_mcp.notion_fetch(session, ref)
            return out, _trace(name, purpose=_purpose("notion fetch", ref), summary=out)

        if name == "github_search":
            query = str(tin.get("query") or "").strip()
            out = await github.search_issues(query)
            return out, _trace(name, purpose=_purpose("github", query), summary=out)

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
