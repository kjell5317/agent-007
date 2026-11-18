"""kotx run prompts and the code-change hand-off proposal.

kotx no longer sends its own Home Assistant approval/merge prompts (see docs/
Home Assistant Notification Handoff) — we drive them from the transition
webhook and dispatch these actions back to the kotx API in the notifications
router. They map to the old kotx HA action ids: KOTX_START ↔
`kotx-task-<id>-start`, KOTX_APPROVE/KOTX_MERGE ↔ `kotx-task-<id>-approve`.
"""

from __future__ import annotations

from app.services.notifications import push
from app.services.notifications.messages import (
    ACTION_DISMISS_TASK,
    clip,
    handoff_tag,
    short_title,
    task_tag,
    task_url,
)
from app.services.notifications.push import Message

ACTION_KOTX_START = "KOTX_START"
ACTION_KOTX_APPROVE = "KOTX_APPROVE"
ACTION_KOTX_MERGE = "KOTX_MERGE"
ACTION_HANDOFF_ACCEPT = "HANDOFF_ACCEPT"
ACTION_HANDOFF_DECLINE = "HANDOFF_DECLINE"

_DISMISS = (ACTION_DISMISS_TASK, "Dismiss")


def kotx_tag(task_id) -> str:
    return task_tag(task_id, "kotx")


async def _prompt(task, body: str, actions: tuple[tuple[str, str], ...]) -> None:
    await push.send(Message(
        title=short_title(task), body=body, tag=kotx_tag(task.id),
        url=task_url(task.id), actions=actions, sticky=True,
    ))


async def notify_kotx_start(task) -> None:
    """A kotx implement run drafted TASK.md and is waiting to start the write phase."""
    await _prompt(task, "Start Implementation", ((ACTION_KOTX_START, "Start"), _DISMISS))


async def notify_kotx_open_pr(task) -> None:
    """A kotx implement run finished coding and proposed a pull request."""
    await _prompt(task, "Open Pull Request", ((ACTION_KOTX_APPROVE, "Open PR"), _DISMISS))


async def notify_kotx_confirm_merge(
    task, *, approved_by: str | None = None, comment: str | None = None
) -> None:
    """An approving PR review moved a tracked implement run back to
    awaiting_approval with a merge proposal. Merge is the only offered action."""
    lines = ["Merge Pull Request"]
    if approved_by:
        lines.append(f"Approved by {approved_by}")
    if comment and comment.strip():
        lines.append(clip(comment, 200))
    await _prompt(task, "\n".join(lines), ((ACTION_KOTX_MERGE, "Merge"),))


async def notify_kotx_review_ready(task, *, assignee: str = "unassigned") -> None:
    """A kotx review run produced REVIEW.md and is waiting on a human decision."""
    await _prompt(task, f"Comment PR of {assignee}", (_DISMISS,))


async def clear_kotx_prompt(task_id) -> None:
    await push.clear(kotx_tag(task_id))


async def notify_handoff_proposed(task, *, brief: str) -> None:
    """A code change the agent can start: GitHub issue + kotx run."""
    await push.send(Message(
        title=f"Open issue? {short_title(task)}",
        body=clip(brief, 300),
        tag=handoff_tag(task.id),
        url=task_url(task.id),
        actions=((ACTION_HANDOFF_ACCEPT, "Open issue"), (ACTION_HANDOFF_DECLINE, "Decline")),
        sticky=True,
    ))
