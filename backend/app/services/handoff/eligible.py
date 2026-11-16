"""Which hand-off kinds the extraction agent is offered, and what survives
of its choice. Pure: no I/O, so it's testable from fixture rows."""

from __future__ import annotations

import re
from collections.abc import Collection
from email.utils import parseaddr
from typing import Any

from app.services.kotx.runs import has_github_url

HANDOFF_KINDS = ("research", "email_reply", "code_change")
# Started right after the task is saved; a code change waits for a tap.
AUTO_KINDS = frozenset({"research", "email_reply"})

_SOURCES = frozenset({"manual", "gmail", "slack"})
_NO_REPLY_RE = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?@|mailer-daemon", re.I)


def handoff_kinds(raw, repo_labels: Collection[str]) -> list[str]:
    """`repo_labels` are the labels connected to a GitHub repo; without any,
    a code change can't be filed."""
    if raw.source not in _SOURCES:
        return []
    kinds = ["research"]
    if replyable(raw):
        kinds.append("email_reply")
    if repo_labels:
        kinds.append("code_change")
    return kinds


def replyable(raw) -> bool:
    meta = raw.source_metadata or {}
    sender = parseaddr(str(meta.get("reply_to") or meta.get("from") or ""))[1]
    return raw.source == "gmail" and bool(sender) and not _NO_REPLY_RE.search(sender)


def valid_handoff(
    choice: Any, raw, *, label: str | None, link: str | None, repo_labels: Collection[str],
) -> dict[str, str] | None:
    """The agent's `handoff` field, checked against the input and the task it
    created: kinds the input wasn't offered are dropped, and a code change
    needs a label connected to a repo and no GitHub link yet."""
    if not isinstance(choice, dict):
        return None
    kind = choice.get("kind")
    brief = " ".join(str(choice.get("brief") or "").split())
    if kind not in handoff_kinds(raw, repo_labels) or not brief:
        return None
    if kind == "code_change" and not code_change_possible(label, link, repo_labels):
        return None
    return {"kind": kind, "brief": brief[:500]}


def code_change_possible(label: str | None, link: str | None, repo_labels: Collection[str]) -> bool:
    return label in repo_labels and not has_github_url(link)


def valid_answer(value: Any) -> str | None:
    answer = str(value or "").strip()
    return answer or None
