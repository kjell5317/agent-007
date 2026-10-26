"""Jev state builders: one input plus the tasks and events it may belong to.

Kept small on purpose — Jev's accuracy drops as unrelated text piles into the
state — and free of I/O so the shapes are testable from fixture objects.
History (NOT_TASK precedents, how the sender's inputs ended) stays out: Jev
handles counts and "a similar input wasn't a task" inferences poorly, and the
triage model (`features.py`) weighs that history from the user's own data.
Candidates and events get neutral ids (A–E, E1–E3) because option labels
sway Jev's answers; `StateRefs` maps the ids back to rows.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

BODY_CHARS = 4000
TASK_DESCRIPTION_CHARS = 300
CANDIDATE_IDS = ("A", "B", "C", "D", "E")
URL_IDS = ("U1", "U2", "U3", "U4")

# Reply/forward prefixes (English, German, Nordic) and leading `[list]` tags,
# repeated: "Re: AW: [team] Fwd: Budget" → "Budget".
_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd|aw|wg|sv|vs)\s*(?:\[\d+\])?\s*:\s*|\[[^\]]{1,60}\]\s*)+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StateRefs:
    title: str | None = None
    candidates: dict[str, uuid.UUID] = field(default_factory=dict)
    events: dict[str, tuple[str, str]] = field(default_factory=dict)
    urls: dict[str, str] = field(default_factory=dict)


def clean_subject(subject: str | None) -> str:
    return " ".join(_SUBJECT_PREFIX_RE.sub("", subject or "").split())


def candidate_title(raw: Any, user_title: str | None = None) -> str | None:
    """The source's own title, judged by Jev (`title_ok`) before any rewrite."""
    if user_title and user_title.strip():
        return user_title.strip()[:200]
    meta = getattr(raw, "source_metadata", None) or {}
    subject = clean_subject(meta.get("subject"))
    if subject:
        return subject[:200]
    first_line = next(
        (line.strip() for line in (getattr(raw, "content", "") or "").splitlines() if line.strip()),
        "",
    )
    limit = 200 if getattr(raw, "source", None) == "manual" else 80
    return first_line[:limit] or None


def message_state(raw: Any) -> dict[str, Any]:
    meta = getattr(raw, "source_metadata", None) or {}
    out: dict[str, Any] = {"source": getattr(raw, "source", None)}
    for key in ("from", "to", "subject", "channel_name"):
        if meta.get(key):
            out[key] = meta[key]
    if "directed_at_me" in meta:
        out["directed_at_me"] = bool(meta["directed_at_me"])
    body = (getattr(raw, "content", "") or "").strip()
    out["body"] = body[:BODY_CHARS] + (" […]" if len(body) > BODY_CHARS else "")
    return out


def clock_state(now: datetime) -> dict[str, str]:
    return {"now": now.isoformat(timespec="minutes"), "weekday": now.strftime("%A")}


def task_candidates_state(
    task_candidates: Sequence[tuple[Any, Any]],
) -> tuple[list[dict[str, Any]], dict[str, uuid.UUID]]:
    """`(hit, task)` pairs in similarity order → state rows and id → task_id."""
    rows: list[dict[str, Any]] = []
    ids: dict[str, uuid.UUID] = {}
    for label, (hit, task) in zip(CANDIDATE_IDS, task_candidates):
        ids[label] = task.id
        row: dict[str, Any] = {
            "id": label,
            "status": hit.status,
            "title": task.title,
            "similarity": round(float(hit.similarity), 2),
        }
        if getattr(task, "due_date", None):
            row["due"] = task.due_date.date().isoformat()
        if getattr(task, "label", None):
            row["label"] = task.label
        if hit.sender:
            row["from"] = hit.sender
        if hit.received_at:
            row["received"] = hit.received_at.date().isoformat()
        rows.append(row)
    return rows, ids


def events_state(events: Sequence[Any]) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
    rows: list[dict[str, Any]] = []
    ids: dict[str, tuple[str, str]] = {}
    for n, event in enumerate(events, start=1):
        if not event.event_id or not event.calendar_id:
            continue
        label = f"E{n}"
        ids[label] = (event.event_id, event.calendar_id)
        row: dict[str, Any] = {"id": label, "title": event.summary}
        if event.starts_at:
            row["start"] = event.starts_at.isoformat(timespec="minutes")
        rows.append(row)
    return rows, ids


def url_options(raw: Any) -> dict[str, str]:
    urls = [
        u
        for u in (getattr(raw, "source_metadata", None) or {}).get("urls") or []
        if isinstance(u, str)
    ]
    return dict(zip(URL_IDS, urls))


def new_input_state(
    raw: Any,
    *,
    now: datetime,
    task_candidates: Sequence[tuple[Any, Any]] = (),
    related_events: Sequence[Any] = (),
    user_title: str | None = None,
) -> tuple[dict[str, Any], StateRefs]:
    title = candidate_title(raw, user_title)
    candidates, candidate_ids = task_candidates_state(task_candidates)
    events, event_ids = events_state(related_events)
    urls = url_options(raw)
    state: dict[str, Any] = {**clock_state(now), "input": message_state(raw)}
    if title:
        state["candidate_title"] = title
    if candidates:
        state["candidates"] = candidates
    if events:
        state["upcoming_events"] = events
    if urls:
        state["links"] = [{"id": key, "url": url} for key, url in urls.items()]
    return state, StateRefs(title=title, candidates=candidate_ids, events=event_ids, urls=urls)


def followup_state(raw: Any, task: Any, *, now: datetime, status: str) -> dict[str, Any]:
    task_row: dict[str, Any] = {"title": task.title, "status": status}
    if getattr(task, "description", None):
        task_row["description"] = task.description.strip()[:TASK_DESCRIPTION_CHARS]
    if getattr(task, "due_date", None):
        task_row["due"] = task.due_date.isoformat(timespec="minutes")
    return {**clock_state(now), "task": task_row, "input": message_state(raw)}


def split_state(task: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"title": task.title}
    if getattr(task, "description", None):
        out["description"] = task.description.strip()[:TASK_DESCRIPTION_CHARS]
    if getattr(task, "estimation", None):
        out["estimate_minutes"] = task.estimation
    return out
