"""Formatting helpers for similar raw-input precedents."""

from __future__ import annotations

import uuid
from typing import Any

from app.agent.helpers.text import task_field_lines
from app.db.clients.raw_inputs import SimilarInput


def task_candidate_lines(
    hit: SimilarInput, task: Any, *, include_existing_task_id: bool = True
) -> list[str]:
    title = truncate_inline(str(getattr(task, "title", "") or "(untitled task)"), 120)
    id_part = (
        f"existing_task_id={task.id}"
        if include_existing_task_id
        else f"task_id={task.id}"
    )
    lines = [
        "",
        f"[{hit.status.upper()}] sim={hit.similarity:.2f} · {id_part} · title: {title}",
    ]
    for line in task_field_lines(task):
        # id/title are already in the header; link and the raw input snippet are
        # dropped as redundant noise for the dedup decision.
        if line.strip().lower().startswith(("id:", "title:", "link:")):
            continue
        lines.append(line)

    meta = candidate_metadata(hit)
    if meta:
        lines.append(f"  metadata: {meta}")

    return lines


def not_task_candidate_lines(hit: SimilarInput) -> list[str]:
    title = candidate_title(hit)
    lines = [
        "",
        f"[NOT_TASK] sim={hit.similarity:.2f} · title: {title}",
    ]

    meta = candidate_metadata(hit)
    if meta:
        lines.append(f"  metadata: {meta}")

    if hit.label:
        lines.append(f"  label: {hit.label}")

    reason = truncate_inline(str((hit.agent_trace or {}).get("reason") or ""), 180)
    if reason:
        lines.append(f"  reason: {reason}")

    return lines


def candidate_title(hit: SimilarInput) -> str:
    subject = truncate_inline(hit.subject or "", 120)
    if subject:
        return subject

    for raw_line in (hit.content_snippet or "").splitlines():
        line = truncate_inline(raw_line, 120)
        if line:
            return line

    sender = truncate_inline(hit.sender or "", 80)
    if sender:
        return f"{hit.source} from {sender}"
    return f"{hit.source} input"


def candidate_trace_ref(hit: SimilarInput) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "ref": f"candidate:{hit.id}",
        "kind": "candidate",
        "id": str(hit.id),
        "status": hit.status,
        "source": hit.source,
        "task_id": str(hit.task_id) if hit.task_id else None,
        "similarity": round(hit.similarity, 4),
        "sim": round(hit.similarity, 4),
        "title": candidate_title(hit),
        "snippet": truncate_inline(hit.content_snippet or "", 300),
        "sender": hit.sender,
        "received_at": hit.received_at.isoformat() if hit.received_at else None,
    }
    if hit.label:
        ref["label"] = hit.label
    return ref


def selected_candidate_ref(
    candidates: list[SimilarInput], task_id: uuid.UUID
) -> str | None:
    for hit in candidates:
        if hit.task_id == task_id:
            return f"candidate:{hit.id}"
    return None


def candidate_metadata(hit: SimilarInput) -> str:
    parts = [f"source={hit.source}"]
    sender = truncate_inline(hit.sender or "", 100)
    if sender:
        parts.append(f"from={sender}")
    if hit.received_at:
        parts.append(f"received_at={hit.received_at.isoformat()}")
    return " · ".join(parts)


def truncate_inline(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
