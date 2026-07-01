"""Prompt-text builders and metadata helpers shared by the agent flows.

The functions here turn a `raw_input` row into the human-readable lines the
agent reads. Pure functions only — no DB access, no LLM calls — so they're
trivially unit-testable from fixture dicts.

Embedding-time text construction lives in `app.services.input.embedding`
(`candidate_query_text`) — kept separate because it feeds the embedding
model rather than the LLM prompt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

AGENT_DATETIME_STEP_MINUTES = 5


def now_iso(tz_name: str | None = None) -> str:
    """Current time as ISO-8601 with an explicit offset.

    Pass `tz_name` (e.g. "Europe/Berlin") so the agent sees wall-clock time
    in the user's zone — otherwise it defaults to UTC and "tomorrow at 2 PM"
    gets stored as 14:00Z, which renders as 16:00 CEST on the frontend.
    """
    tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    return ceil_datetime_to_minute_step(datetime.now(tz)).isoformat()


def parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    # Accept trailing Z for UTC, which fromisoformat doesn't.
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def ceil_datetime_to_minute_step(
    value: datetime,
    *,
    minutes: int = AGENT_DATETIME_STEP_MINUTES,
) -> datetime:
    """Return `value` rounded up to the next minute-step boundary."""
    if minutes <= 0:
        raise ValueError("minutes must be positive")

    elapsed = timedelta(
        minutes=value.minute % minutes,
        seconds=value.second,
        microseconds=value.microsecond,
    )
    if elapsed == timedelta(0):
        return value.replace(second=0, microsecond=0)
    return (value + (timedelta(minutes=minutes) - elapsed)).replace(
        second=0,
        microsecond=0,
    )


def normalize_agent_due_date(value: str | datetime | None) -> datetime | None:
    """Parse and round agent-emitted due dates to deterministic 5-minute steps."""
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return ceil_datetime_to_minute_step(parsed)


def task_field_lines(task, *, indent: str = "  ") -> list[str]:
    """Render an existing task's fields for the agent prompt.

    Shared by the thread-follow-up flow (single linked task) and the
    new-input flow (each duplicate candidate), so the agent sees enough
    detail to decide whether to update, close, or leave a task alone.
    """
    lines = [f"{indent}id: {task.id}", f"{indent}title: {task.title}"]
    if task.description:
        desc = task.description.strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:200] + "…"
        lines.append(f"{indent}description: {desc}")
    if task.due_date:
        lines.append(f"{indent}due_date: {task.due_date.isoformat()}")
    if task.estimation is not None:
        lines.append(f"{indent}estimation: {task.estimation} min")
    if task.location:
        lines.append(f"{indent}location: {task.location}")
    if task.link:
        lines.append(f"{indent}link: {task.link}")
    if task.label:
        lines.append(f"{indent}label: {task.label}")
    return lines


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
