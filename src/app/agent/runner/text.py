"""Prompt-text builders and metadata helpers shared by the agent flows.

The functions here turn a `raw_input` row into the human-readable lines the
agent reads. Pure functions only — no DB access, no LLM calls — so they're
trivially unit-testable from fixture dicts.
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    # Accept trailing Z for UTC, which fromisoformat doesn't.
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def candidate_query_text(content: str, metadata: dict) -> str:
    parts: list[str] = []
    sender = _sender_descriptor(metadata)
    if sender:
        parts.append(f"from: {sender}")
    subject = metadata.get("subject")
    if subject:
        parts.append(subject)
    body = (content or "").strip()
    if body:
        parts.append(body[:1500])
    return "\n".join(parts).strip()


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


def _sender_descriptor(metadata: dict) -> str | None:
    """Source-agnostic 'from' value for the embedding.

    Always labeled `from:` in the query text, regardless of source, so the
    embedding doesn't get a categorical Gmail/Slack split from the key name.
    For Slack we fold the channel into the same line (`alice in #general`)
    since channel context is part of what makes repeated alerts cluster.
    """
    sender = (metadata.get("from") or "").strip() or None
    channel = (metadata.get("channel_name") or "").strip() or None
    if sender and channel:
        return f"{sender} in {channel}"
    return sender or channel
