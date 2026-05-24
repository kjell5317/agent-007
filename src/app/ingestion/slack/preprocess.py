"""Preprocess Slack messages into clean text + structured metadata.

Slack messages arrive as JSON with `text` in mrkdwn — a flavor of markdown
that uses Slack-specific link syntax (`<@U123>`, `<#C123|name>`, `<url|label>`).
This module:

  1. Resolves user/channel/link refs into plain text (when names are known).
  2. Extracts URLs into structured metadata.
  3. Collapses whitespace.

Pure functions only — no network, no DB, no global state — so unit-testable
from fixture dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_BODY_CHARS = 8000

# Slack link types in mrkdwn:
#   <@U123>            — user mention
#   <@U123|name>       — user mention with cached label
#   <#C123>            — channel mention
#   <#C123|name>       — channel mention with cached label
#   <https://...>      — bare URL
#   <https://...|text> — labeled URL
#   <mailto:x@y|x@y>   — mailto
_USER_REF_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL_REF_RE = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]+))?>")
_LINK_REF_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
_MAILTO_REF_RE = re.compile(r"<mailto:([^|>]+)(?:\|([^>]+))?>")


@dataclass
class PreprocessResult:
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False


def preprocess_message(
    raw_message: dict,
    *,
    channel_id: str,
    channel_name: str | None = None,
    user_names: dict[str, str] | None = None,
) -> PreprocessResult:
    """Turn a Slack message into clean body text + metadata.

    `user_names` maps user IDs to display names so mentions render as
    `@alice` instead of `@U123`. Pass an empty dict for tests.
    """
    user_names = user_names or {}

    text = raw_message.get("text") or ""
    text, urls = _normalize_refs(text, user_names)
    text = _collapse_whitespace(text)

    truncated = False
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS].rstrip() + "\n[...truncated]"
        truncated = True

    user_id = raw_message.get("user")
    metadata: dict[str, Any] = {
        "from": user_names.get(user_id, user_id),
        "from_id": user_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": raw_message.get("ts"),
        # `thread_ts` is the parent message's ts. Use it as `thread_id` so the
        # agent's Gmail-style thread-shortcut works the same way.
        "thread_id": raw_message.get("thread_ts") or raw_message.get("ts"),
        "subtype": raw_message.get("subtype"),
        "bot_id": raw_message.get("bot_id"),
        "urls": urls,
    }
    return PreprocessResult(body=text, metadata=metadata, truncated=truncated)


def _normalize_refs(text: str, user_names: dict[str, str]) -> tuple[str, list[str]]:
    """Replace Slack mrkdwn refs with plain text; return (text, distinct urls)."""
    urls: list[str] = []
    seen: set[str] = set()

    def _user(m: re.Match) -> str:
        uid = m.group(1)
        label = m.group(2) or user_names.get(uid)
        return f"@{label}" if label else f"@{uid}"

    def _channel(m: re.Match) -> str:
        label = m.group(2) or m.group(1)
        return f"#{label}"

    def _link(m: re.Match) -> str:
        url, label = m.group(1), m.group(2)
        if url not in seen:
            seen.add(url)
            urls.append(url)
        return f"{label} ({url})" if label and label != url else url

    def _mailto(m: re.Match) -> str:
        addr = m.group(1)
        return m.group(2) or addr

    text = _USER_REF_RE.sub(_user, text)
    text = _CHANNEL_REF_RE.sub(_channel, text)
    text = _LINK_REF_RE.sub(_link, text)
    text = _MAILTO_REF_RE.sub(_mailto, text)
    return text, urls


def _collapse_whitespace(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if not line:
            blanks += 1
            if blanks <= 1:
                out.append("")
        else:
            blanks = 0
            out.append(line)
    return "\n".join(out).strip()
