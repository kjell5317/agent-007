"""Pure normalization of kotx task payloads into raw-input envelopes.

No I/O — document fetching happens in the source; this module only shapes
data so it is unit-testable from fixture dicts.
"""

from __future__ import annotations

import re
from typing import Any

from app.db.schemas.raw_input import RawInputCreate

# All kotx run kinds should surface in the inbox. Automatic resolve-conflict
# runs stay informational unless a terminal transition matches an existing task.
INGESTED_KINDS = frozenset({"implement", "review", "resolve_conflict"})

# States where the user must act — these carry the brief and may create a task.
ACTIONABLE = frozenset(
    {("implement", "draft"), ("implement", "awaiting_approval"), ("review", "awaiting_approval")}
)

# States that complete the linked 007 task: merged/terminal for implement and
# a sent review (awaiting_external) for review tasks. resolve_conflict runs
# are auxiliary — resolving conflicts never completes the tracked work, so
# their terminal transitions stay informational (no close, no points); the
# task closes when its own implement/review run ends.
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


def display_assignee_for(task: dict) -> str:
    """First non-empty PR assignee from kotx, or a stable fallback."""
    for field in ("assigned", "assignees"):
        values = task.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            assignee = str(value or "").strip()
            if assignee:
                return assignee
    return "unassigned"


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

    return RawInputCreate(
        source="kotx",
        external_id=f"{kotx_id}:{task.get('attempt') or 1}:{state}:{proposes}",
        content=doc[:6000] if doc else "",
        source_metadata={
            "thread_id": github_thread_key(repo, number),
            "kotx_task_id": kotx_id,
            "kotx_kind": kind,
            "kotx_state": state,
            "kotx_proposes": task.get("proposes"),
            "kotx_status": task.get("status"),
            "state_reason": task.get("stateReason"),
            "trigger_reason": task.get("triggerReason"),
            "repo": repo,
            "subject_type": task.get("subjectType"),
            "subject_number": number,
            "github_url": task.get("githubUrl"),
            "pr_number": task.get("prNumber") or task.get("trackedPrNumber"),
            "branch": task.get("branch"),
            "assignee": display_assignee_for(task),
            "subject": f"{repo}#{number} {title}",
        },
    )


def sort_key(task: dict) -> Any:
    return task.get("updatedAt") or ""
