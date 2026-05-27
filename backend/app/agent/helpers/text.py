"""Prompt-text builders and metadata helpers shared by the agent flows.

The functions here turn a `raw_input` row into the human-readable lines the
agent reads. Pure functions only — no DB access, no LLM calls — so they're
trivially unit-testable from fixture dicts.

Embedding-time text construction lives in `app.services.input.embedding`
(`candidate_query_text`) — kept separate because it feeds the embedding
model rather than Claude.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now_iso(tz_name: str | None = None) -> str:
    """Current time as ISO-8601 with an explicit offset.

    Pass `tz_name` (e.g. "Europe/Berlin") so the agent sees wall-clock time
    in the user's zone — otherwise it defaults to UTC and "tomorrow at 2 PM"
    gets stored as 14:00Z, which renders as 16:00 CEST on the frontend.
    """
    tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    return datetime.now(tz).replace(microsecond=0).isoformat()


def parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    # Accept trailing Z for UTC, which fromisoformat doesn't.
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# Metadata keys surfaced to the agent. Source-agnostic: missing keys are
# silently skipped (Gmail has subject/to/cc; Slack has channel_name; etc).
_META_KEYS = ("from", "to", "cc", "subject", "channel_name", "date", "thread_id")


def append_meta_lines(lines: list[str], meta: dict, *, include_account: bool = False) -> None:
    keys = _META_KEYS + (("account",) if include_account else ())
    for key in keys:
        val = meta.get(key)
        if val:
            lines.append(f"{key.replace('_', ' ').capitalize()}: {val}")
    if "directed_at_me" in meta:
        lines.append(f"Directed at me: {'yes' if meta['directed_at_me'] else 'no'}")
    if meta.get("has_attachments"):
        lines.append("Has attachments: yes")
    urls = meta.get("urls") or []
    if urls:
        lines.append("Links (use one of these as `link`):")
        for u in urls[:4]:
            lines.append(f"  - {u}")
