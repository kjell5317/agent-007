"""Which hand-off kinds the extraction agent is offered, and what survives
of its choice. Pure: no I/O, so it's testable from fixture rows."""

from __future__ import annotations

import re
from email.utils import parseaddr
from typing import Any

from app.agent.helpers.web import task_urls
from app.services.kotx.runs import KOTX_ISSUE_REPOS, has_github_url

HANDOFF_KINDS = ("web_research", "own_data_lookup", "email_reply", "code_change")
# Started right after the task is saved; anything else waits for a tap.
AUTO_KINDS = frozenset({"web_research", "own_data_lookup", "email_reply"})

_SOURCES = frozenset({"manual", "gmail", "slack"})
_NO_REPLY_RE = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?@|mailer-daemon", re.I)


def handoff_kinds(raw) -> list[str]:
    if raw.source not in _SOURCES:
        return []
    kinds = ["web_research", "own_data_lookup", "code_change"]
    if raw.source == "gmail" and not _NO_REPLY_RE.search(_sender(raw)):
        kinds.insert(2, "email_reply")
    return kinds


def _sender(raw) -> str:
    meta = raw.source_metadata or {}
    return parseaddr(str(meta.get("reply_to") or meta.get("from") or ""))[1]


def valid_handoff(choice: Any, raw, *, label: str | None, link: str | None) -> dict[str, str] | None:
    """The agent's `handoff` field, checked against the input and the task it
    created. Drops kinds the input wasn't offered and code changes the kotx
    mapping can't take; a `url` not among the input's links falls back to
    query-mode research."""
    if not isinstance(choice, dict):
        return None
    kind = choice.get("kind")
    brief = " ".join(str(choice.get("brief") or "").split())
    if kind not in handoff_kinds(raw) or not brief:
        return None
    if kind == "code_change" and (label not in KOTX_ISSUE_REPOS or has_github_url(link)):
        return None
    out = {"kind": kind, "brief": brief[:500]}
    url = str(choice.get("url") or "").strip()
    if kind == "web_research" and url in task_urls(raw):
        out["url"] = url
    return out
