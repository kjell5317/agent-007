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
from datetime import datetime, timezone
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
_SPECIAL_REF_RE = re.compile(r"<!([^>|]+)(?:\|([^>]+))?>")
_LINK_REF_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]+))?>")
_MAILTO_REF_RE = re.compile(r"<mailto:([^|>]+)(?:\|([^>]+))?>")
_MARKDOWN_LINK_RE = re.compile(
    r"""!?                 # optional image marker; keep only the target either way
    \[[^\]]*]              # link title
    \(
        \s*
        ([a-z][a-z0-9+.-]*:[^\s)]+)
        (?:\s+["'][^)]*["'])?
        \s*
    \)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_EMOJI_RE = re.compile(r"(?<!\w):[A-Za-z0-9_+\-]+:(?!\w)")
_QUOTE_MARKER_RE = re.compile(r"(?m)^\s*>\s?")
_CODE_DECORATOR_RE = re.compile(r"(`{1,3})(.*?)\1", re.DOTALL)
_EMPHASIS_DECORATOR_RES = [
    re.compile(r"(?<!\w)(\*{1,3})(?=\S)(.*?)(?<=\S)\1(?!\w)"),
    re.compile(r"(?<!\w)(_{1,3})(?=\S)(.*?)(?<=\S)\1(?!\w)"),
    re.compile(r"(?<!\w)(~{1,2})(?=\S)(.*?)(?<=\S)\1(?!\w)"),
]


@dataclass
class PreprocessResult:
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False
    received_at: datetime | None = None


def preprocess_message(
    raw_message: dict,
    *,
    channel_id: str,
    channel_name: str | None = None,
    workspace_name: str | None = None,
    user_names: dict[str, str] | None = None,
    authed_user_id: str | None = None,
    is_dm: bool = False,
) -> PreprocessResult:
    """Turn a Slack message into clean body text + metadata.

    `user_names` maps user IDs to display names so mentions render as
    `@alice` instead of `@U123`. Pass an empty dict for tests.

    `authed_user_id` and `is_dm` feed the `directed_at_me` metadata flag.
    """
    user_names = user_names or {}

    raw_text = raw_message.get("text") or ""
    directed = _directed_at_me(raw_text, authed_user_id, is_dm)

    text, urls = _normalize_refs(raw_text, user_names)
    text = _strip_emoji_shortcodes(text)
    text = _strip_markdown_decorators(text)
    text = _collapse_whitespace(text)

    truncated = False
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS].rstrip() + "\n[...truncated]"
        truncated = True

    user_id = raw_message.get("user")
    sender = user_names.get(str(user_id), user_id)
    metadata: dict[str, Any] = {
        "from": _append_workspace_name(sender, workspace_name),
        "channel_id": channel_id,
        "channel_name": channel_name,
        "thread_id": raw_message.get("thread_ts") or raw_message.get("ts"),
        "subtype": raw_message.get("subtype"),
        "bot_id": raw_message.get("bot_id"),
        "urls": urls,
        "is_dm": is_dm,
        "directed_at_me": directed,
    }
    return PreprocessResult(
        body=text,
        metadata=metadata,
        truncated=truncated,
        received_at=_received_at(raw_message),
    )


def _received_at(raw_message: dict) -> datetime | None:
    """A Slack message `ts` is epoch-seconds (with microseconds) of when it was
    posted — its identity and its original time."""
    ts = raw_message.get("ts")
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def _append_workspace_name(sender: str | None, workspace_name: str | None) -> str | None:
    if not sender:
        return None
    workspace = workspace_name.strip() if workspace_name else ""
    if not workspace:
        return sender
    return f"{sender} ({workspace})"


# DM is treated as direct. `<@user_id>` and `<!channel>` are the literal
# mrkdwn for @user and @channel respectively — checked on raw text before
# `_normalize_refs` rewrites them.
_DIRECT_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]+)?>")
_CHANNEL_BROADCAST = "<!channel>"


def _directed_at_me(text: str, authed_user_id: str | None, is_dm: bool) -> bool:
    if is_dm:
        return True
    if not text:
        return False
    if _CHANNEL_BROADCAST in text:
        return True
    if authed_user_id:
        for m in _DIRECT_MENTION_RE.finditer(text):
            if m.group(1) == authed_user_id:
                return True
    return False


def _normalize_refs(text: str, user_names: dict[str, str]) -> tuple[str, list[str]]:
    """Replace Slack mrkdwn refs with plain text; return (text, distinct urls)."""
    urls: list[str] = []
    seen: set[str] = set()

    def _remember_url(url: str) -> None:
        if url not in seen:
            seen.add(url)
            urls.append(url)

    def _user(m: re.Match) -> str:
        uid = m.group(1)
        label = m.group(2) or user_names.get(uid)
        return f"@{label}" if label else f"@{uid}"

    def _channel(m: re.Match) -> str:
        label = m.group(2) or m.group(1)
        return f"#{label}"

    def _special(m: re.Match) -> str:
        label = m.group(2) or m.group(1)
        label = label.removeprefix("!")
        return label if label.startswith("@") else f"@{label}"

    def _link(m: re.Match) -> str:
        url = m.group(1)
        _remember_url(url)
        return url

    def _mailto(m: re.Match) -> str:
        addr = m.group(1)
        return addr

    def _markdown_link(m: re.Match) -> str:
        url = m.group(1)
        if url.startswith(("http://", "https://")):
            _remember_url(url)
        return url

    text = _USER_REF_RE.sub(_user, text)
    text = _CHANNEL_REF_RE.sub(_channel, text)
    text = _SPECIAL_REF_RE.sub(_special, text)
    text = _LINK_REF_RE.sub(_link, text)
    text = _MAILTO_REF_RE.sub(_mailto, text)
    text = _MARKDOWN_LINK_RE.sub(_markdown_link, text)
    return text, urls


def _strip_emoji_shortcodes(text: str) -> str:
    return _EMOJI_RE.sub("", text)


def _strip_markdown_decorators(text: str) -> str:
    text = _QUOTE_MARKER_RE.sub("", text)
    text = _CODE_DECORATOR_RE.sub(r"\2", text)
    for pattern in _EMPHASIS_DECORATOR_RES:
        text = pattern.sub(r"\2", text)
    return text


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
