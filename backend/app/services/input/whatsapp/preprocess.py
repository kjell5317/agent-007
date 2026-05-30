"""Parse WhatsApp Cloud API webhook payloads into normalized messages.

Meta delivers inbound messages under `entry[].changes[].value.messages[]`;
delivery/read receipts arrive under `value.statuses[]` and are ignored. For
text-only ingestion we keep `text` bodies and media *captions*; messages with
no text (voice notes, stickers, locations, …) are dropped.

Payload shape (trimmed):
    {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "..."},
        "contacts": [{"profile": {"name": "Ada"}, "wa_id": "4915..."}],
        "messages": [{"from": "4915...", "id": "wamid...",
                      "timestamp": "1716...", "type": "text",
                      "text": {"body": "..."}}]}}]}]}

Pure functions only — no network, no DB — so unit-testable from fixture dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_BODY_CHARS = 8000

# Media types whose `caption` we treat as the body. Other non-text types
# (audio/voice, sticker, location, contacts, reaction) have no caption and are
# skipped in text-only mode.
_CAPTIONED_TYPES = frozenset({"image", "video", "document"})

_WHITESPACE_RUN_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass
class ParsedMessage:
    message_id: str  # wamid... — stable dedup key
    body: str
    from_number: str
    sender_name: str | None
    type: str
    timestamp: str | None
    phone_number_id: str | None
    truncated: bool = False


def parse_messages(payload: dict) -> list[ParsedMessage]:
    out: list[ParsedMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            names = _contact_names(value.get("contacts") or [])
            phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
            for msg in value.get("messages") or []:
                parsed = _parse_one(msg, names, phone_number_id)
                if parsed is not None:
                    out.append(parsed)
    return out


def _contact_names(contacts: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for c in contacts:
        wa_id = c.get("wa_id")
        name = (c.get("profile") or {}).get("name")
        if wa_id and name:
            names[wa_id] = name
    return names


def _parse_one(
    msg: dict, names: dict[str, str], phone_number_id: str | None
) -> ParsedMessage | None:
    message_id = msg.get("id")
    from_number = msg.get("from")
    if not message_id or not from_number:
        return None

    raw_body = _extract_body(msg)
    if not raw_body:
        return None
    body, truncated = _clean(raw_body)
    if not body:
        return None

    return ParsedMessage(
        message_id=message_id,
        body=body,
        from_number=from_number,
        sender_name=names.get(from_number),
        type=msg.get("type", "unknown"),
        timestamp=msg.get("timestamp"),
        phone_number_id=phone_number_id,
        truncated=truncated,
    )


def _extract_body(msg: dict) -> str | None:
    msg_type = msg.get("type")
    if msg_type == "text":
        return (msg.get("text") or {}).get("body")
    if msg_type in _CAPTIONED_TYPES:
        return (msg.get(msg_type) or {}).get("caption")
    return None


def _clean(text: str) -> tuple[str, bool]:
    text = _WHITESPACE_RUN_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text).strip()
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS], True
    return text, False
