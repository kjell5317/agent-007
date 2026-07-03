"""Pure normalization of kotx task payloads into raw-input envelopes.

No I/O — document fetching happens in the source; this module only shapes
data so it is unit-testable from fixture dicts.
"""

from __future__ import annotations

import re
from typing import Any

from app.db.schemas.raw_input import RawInputCreate

# resolve_conflict runs are fully automatic — they never surface in 007.
INGESTED_KINDS = frozenset({"implement", "review"})

# States where the user must act — these carry the brief and may create a task.
ACTIONABLE = frozenset(
    {("implement", "draft"), ("implement", "awaiting_approval"), ("review", "awaiting_approval")}
)

# States that complete the 007 task: merged/terminal for implement, and a
# sent review (awaiting_external) for review tasks.
DONE_STATES = frozenset(
    {
        ("implement", "done"),
        ("implement", "cancelled"),
        ("review", "awaiting_external"),
        ("review", "done"),
        ("review", "cancelled"),
    }
)

_GITHUB_SUBJECT_RE = re.compile(
    r"github\.com/([^/\s]+/[^/\s#]+)/(?:issues|pull)/(\d+)(?:\D|$)"
)


def github_thread_key(repo: str, number: int | str) -> str:
    return f"github:{repo}#{number}"


def parse_github_subject(url: str) -> tuple[str, int] | None:
    """Extract (owner/repo, number) from a GitHub issue/PR URL."""
    m = _GITHUB_SUBJECT_RE.search(url or "")
    if not m:
        return None
    return m.group(1), int(m.group(2))


def is_ingested(task: dict) -> bool:
    return task.get("kind") in INGESTED_KINDS


def brief_doc_for(task: dict) -> str | None:
    """Which document endpoint carries the brief for an actionable state."""
    kind, state = task.get("kind"), task.get("state")
    if (kind, state) not in ACTIONABLE:
        return None
    return "review" if kind == "review" else "task"


def envelope_for_transition(task: dict, doc: str | None = None) -> RawInputCreate | None:
    """One envelope per (task, attempt, state, proposal) — repeated deliveries
    of the same transition dedupe on external_id."""
    if not is_ingested(task):
        return None
    kotx_id = task.get("id")
    repo = str(task.get("repo") or "")
    number = task.get("subjectNumber")
    if kotx_id is None or not repo or number is None:
        return None

    state = str(task.get("state") or "")
    proposes = task.get("proposes") or ""
    title = str(task.get("title") or f"{repo}#{number}")
    kind = str(task.get("kind") or "")

    lines = [
        f"kotx {kind} · {repo}#{number} — {title}",
        f"State: {task.get('status') or state}"
        + (f" (proposes {proposes})" if proposes else ""),
    ]
    if task.get("stateReason"):
        lines.append(f"Reason: {task['stateReason']}")
    if doc:
        lines.append("")
        lines.append(doc[:6000])

    return RawInputCreate(
        source="kotx",
        external_id=f"{kotx_id}:{task.get('attempt') or 1}:{state}:{proposes}",
        content="\n".join(lines),
        source_metadata={
            "thread_id": github_thread_key(repo, number),
            "kotx_task_id": kotx_id,
            "kotx_kind": kind,
            "kotx_state": state,
            "kotx_proposes": task.get("proposes"),
            "kotx_status": task.get("status"),
            "state_reason": task.get("stateReason"),
            "repo": repo,
            "subject_type": task.get("subjectType"),
            "subject_number": number,
            "github_url": task.get("githubUrl"),
            "pr_number": task.get("prNumber") or task.get("trackedPrNumber"),
            "branch": task.get("branch"),
            "subject": f"{repo}#{number} {title}",
        },
    )


def sort_key(task: dict) -> Any:
    return task.get("updatedAt") or ""
