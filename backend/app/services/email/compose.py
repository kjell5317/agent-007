"""Build the RFC 2822 reply a draft is made of. Pure.

Drafts always come from the account's primary address, whichever of the
user's addresses the mail was sent to. Recipients come only from the original
message's headers, never from the model or the body, so text in the thread
can't add anyone to the reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, getaddresses

from bs4 import BeautifulSoup

_REPLY_PREFIX = re.compile(r"^\s*(re|aw|sv|antw)\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class Sender:
    address: str
    from_header: str
    signature: str


def primary_sender(send_as: list[dict], account: str) -> Sender:
    """The primary "Send mail as" identity, with its display name and signature."""
    entry = next((e for e in send_as if e.get("isPrimary")), None) or {"sendAsEmail": account}
    address = str(entry["sendAsEmail"])
    name = str(entry.get("displayName") or "")
    return Sender(
        address=address,
        from_header=formataddr((name, address)) if name else address,
        signature=_signature_text(str(entry.get("signature") or "")),
    )


def _signature_text(signature_html: str) -> str:
    if not signature_html.strip():
        return ""
    text = BeautifulSoup(signature_html, "lxml").get_text("\n")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def reply_recipient(meta: dict) -> str:
    return str(meta.get("reply_to") or meta.get("from") or "").strip()


def recipient_address(header: str) -> str:
    pairs = getaddresses([header])
    return pairs[0][1].lower() if pairs and pairs[0][1] else ""


def reply_subject(subject: str | None) -> str:
    subject = (subject or "").strip()
    return subject if _REPLY_PREFIX.match(subject) else f"Re: {subject}".rstrip()


def reply_references(meta: dict) -> str | None:
    message_id = meta.get("message_id_header")
    if not message_id:
        return None
    previous = str(meta.get("references") or "").split()
    return " ".join([*previous, message_id])


def quote_original(body: str, *, sender: str | None, date: str | None) -> str:
    intro = f"On {date}, {sender} wrote:" if date and sender else "Original message:"
    quoted = "\n".join(f"> {line}" if line else ">" for line in body.strip().splitlines())
    return f"{intro}\n{quoted}"


def build_reply(
    *, from_header: str, meta: dict, body: str, signature: str, original_body: str,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = from_header
    msg["To"] = reply_recipient(meta)
    msg["Subject"] = reply_subject(meta.get("subject"))
    if meta.get("message_id_header"):
        msg["In-Reply-To"] = meta["message_id_header"]
        msg["References"] = reply_references(meta)
    parts = [body.strip()]
    if signature:
        parts.append(f"-- \n{signature}")
    parts.append(quote_original(original_body, sender=meta.get("from"), date=meta.get("date")))
    msg.set_content("\n\n".join(parts) + "\n")
    return msg.as_bytes()
