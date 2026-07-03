"""Preprocess Gmail messages into clean text + structured metadata.

The Gmail API returns a deeply nested resource (`users.messages.get`, format=full)
with base64url-encoded bodies and MIME parts. The agent expects compact, plain
text plus per-source metadata. This module performs the conversion:

  1. Walk MIME parts; prefer `text/plain`, fall back to `text/html`.
  2. Convert HTML → text (BeautifulSoup).
  3. Strip quoted replies (`>`, "On <date> ... wrote:", "----- Original Message -----").
  4. Strip signatures (`-- \\n` sigdash + common phrases like "Sent from my ...").
  5. Collapse whitespace and trim.
  6. Extract URLs (kept inline AND surfaced as structured metadata).

Pure functions only — no network, no DB, no global state — so the whole
pipeline is unit-testable from a fixture dict.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any

from bs4 import BeautifulSoup

# Cap on body size kept after preprocessing — protects the agent's context
# from huge marketing emails. Tune as needed.
MAX_BODY_CHARS = 8000

# Catch http(s) and bare www. URLs without trailing punctuation.
_URL_RE = re.compile(
    r"""(?xi)
    \b(
        (?:https?://|www\.)         # scheme or www prefix
        [^\s<>"']+?                 # body (non-greedy, no whitespace/brackets/quotes)
    )
    (?=[\s<>"'\.,;:!?\)\]]*(?:\s|$))  # trailing punctuation/whitespace boundary
    """
)

# Reply-quote intro lines emitted by common clients. Keep these conservative —
# false positives lose real content. Localized intros (de/fr/...) can be
# added here as we hit them.
_REPLY_INTRO_PATTERNS = [
    re.compile(r"^On .{1,200}\s+wrote:\s*$", re.IGNORECASE),
    re.compile(r"^Am .{1,200}\s+schrieb .{1,200}:\s*$", re.IGNORECASE),  # de
    re.compile(r"^Le .{1,200}\s+a écrit\s*:\s*$", re.IGNORECASE),         # fr
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^From:\s.+$", re.IGNORECASE),  # Outlook-style header block start
]

# Standard sigdash per RFC 3676 (`-- ` with trailing space) plus common phrases.
# Em-dash separators (`—`) are deliberately NOT here: GitHub uses them before
# the "Reply to this email directly, view it on GitHub" footer, which carries
# the URLs we want.
_SIGNATURE_PATTERNS = [
    re.compile(r"^-- \s*$"),
    re.compile(r"^Sent from my .+$", re.IGNORECASE),
    re.compile(r"^Get Outlook for .+$", re.IGNORECASE),
]


@dataclass
class PreprocessResult:
    """Output of `preprocess_message`. Hands `body` to the agent and `metadata`
    into `RawInput.source_metadata` for storage + future feedback context."""

    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    truncated: bool = False


def preprocess_message(
    raw_message: dict,
    *,
    account_email: str | None = None,
) -> PreprocessResult:
    """Turn a Gmail API message resource into clean body text + metadata.

    `raw_message` is the JSON dict returned by `users.messages.get` with
    `format=full`. Robust against missing fields — Gmail sometimes returns
    very minimal payloads (e.g. deleted or filtered messages).

    `account_email` is the address of the connected Gmail account; passed
    in so we can compute `directed_at_me` in the metadata.
    """
    payload = raw_message.get("payload") or {}
    headers = _index_headers(payload.get("headers") or [])

    plain, html = _extract_bodies(payload)
    body = plain if plain else (_html_to_text(html) if html else "")

    body = _strip_quoted_replies(body)
    body = _strip_signature(body)
    body = _collapse_whitespace(body)

    # URL extraction from body text covers the common case. For HTML-only links
    # (e.g. GitHub's "View it on GitHub" anchor that doesn't appear in the
    # plain-text alternative) we additionally scan <a href> in the HTML part.
    urls = _extract_urls(body)
    if html:
        urls = _merge_urls(urls, _extract_html_hrefs(html))

    truncated = False
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS].rstrip() + "\n[...truncated]"
        truncated = True

    metadata: dict[str, Any] = {
        "from": headers.get("from"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "reply_to": headers.get("reply-to"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "message_id_header": headers.get("message-id"),
        "thread_id": raw_message.get("threadId"),
        "label_ids": raw_message.get("labelIds", []),
        "urls": urls,
        "has_attachments": _has_attachments(payload),
        "directed_at_me": _directed_at_me(headers, account_email),
    }
    _apply_github_identity(metadata, headers)
    return PreprocessResult(body=body, metadata=metadata, truncated=truncated)


# GitHub notification emails get a canonical cross-source thread key so they
# fold onto the same task as kotx transitions and other emails about the same
# issue/PR — regardless of how Gmail threads them.
_GITHUB_SUBJECT_URL_RE = re.compile(
    r"github\.com/([^/\s]+/[^/\s#?]+)/(?:issues|pull)/(\d+)(?:\D|$)"
)


def _apply_github_identity(metadata: dict[str, Any], headers: dict[str, str]) -> None:
    sender = (metadata.get("from") or "").lower()
    reason = headers.get("x-github-reason")
    if "notifications@github.com" not in sender and not reason:
        return
    if reason:
        metadata["github_reason"] = reason
    for url in metadata.get("urls") or []:
        m = _GITHUB_SUBJECT_URL_RE.search(url)
        if m:
            repo, number = m.group(1), int(m.group(2))
            metadata["github_repo"] = repo
            metadata["github_number"] = number
            metadata["gmail_thread_id"] = metadata.get("thread_id")
            metadata["thread_id"] = f"github:{repo}#{number}"
            return


def _directed_at_me(headers: dict[str, str], account_email: str | None) -> bool:
    """True when the user is one of at most two direct (To:) recipients.

    Returns False when the user only appears in Cc, when To+Cc exceeds two
    distinct addresses, or when `account_email` is unknown.
    """
    if not account_email:
        return False
    me = account_email.strip().lower()
    to = {a.lower() for _, a in getaddresses([headers.get("to") or ""]) if a}
    cc = {a.lower() for _, a in getaddresses([headers.get("cc") or ""]) if a}
    if me not in to:
        return False
    return len(to | cc) <= 2


# --- MIME walk ----------------------------------------------------------------

def _index_headers(headers: list[dict]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in headers if "name" in h and "value" in h}


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """Return (plain_text, html) by walking the MIME tree.

    Concatenates multiple parts of the same type — multipart/mixed with
    inline forwards can produce more than one text/plain part.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _walk(payload, plain_parts, html_parts)
    return "\n".join(plain_parts), "\n".join(html_parts)


def _walk(part: dict, plain_parts: list[str], html_parts: list[str]) -> None:
    mime = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    data = body.get("data")

    if data:
        decoded = _decode_body(data)
        if mime == "text/plain":
            plain_parts.append(decoded)
        elif mime == "text/html":
            html_parts.append(decoded)

    for child in part.get("parts") or []:
        _walk(child, plain_parts, html_parts)


def _decode_body(data: str) -> str:
    """Gmail bodies are URL-safe base64 with padding stripped."""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def _has_attachments(payload: dict) -> bool:
    """True if any MIME part has a non-empty filename."""
    if (payload.get("filename") or "").strip():
        return True
    for child in payload.get("parts") or []:
        if _has_attachments(child):
            return True
    return False


# --- HTML → text --------------------------------------------------------------

def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # Drop content the user never sees but that bs4 would otherwise inline.
    for tag in soup(["script", "style", "head", "meta", "link"]):
        tag.decompose()

    # Preserve link targets as inline "[text](url)" so URL extraction still works.
    for a in soup.find_all("a"):
        href = a.get("href")
        text = a.get_text(strip=True)
        if href and text and href != text:
            a.replace_with(f"{text} ({href})")

    # `\n` separator keeps block-level structure roughly intact.
    return soup.get_text(separator="\n")


# --- Quote / signature stripping ---------------------------------------------

def _strip_quoted_replies(text: str) -> str:
    """Remove the quoted history below a reply.

    Cut on either:
      - a reply-intro line ("On X, Y wrote:", etc.), OR
      - the first of a block of 2+ consecutive `>`-quoted lines.

    The 2-line minimum avoids killing legitimate single-line markdown quotes
    that appear inside the message body.
    """
    lines = text.splitlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(p.match(stripped) for p in _REPLY_INTRO_PATTERNS):
            cut = i
            break
        if stripped.startswith(">") and i + 1 < len(lines) and lines[i + 1].lstrip().startswith(">"):
            cut = i
            break
    return "\n".join(lines[:cut])


def _strip_signature(text: str) -> str:
    """Drop everything from the first signature delimiter onward."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if any(p.match(line) for p in _SIGNATURE_PATTERNS):
            return "\n".join(lines[:i])
    return text


def _collapse_whitespace(text: str) -> str:
    """Trim trailing whitespace per line and collapse 3+ blank lines to one."""
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


# --- URL extraction -----------------------------------------------------------

def _extract_urls(text: str) -> list[str]:
    """Distinct URLs in order of first appearance."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(1).rstrip(").,;:!?")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _extract_html_hrefs(html: str) -> list[str]:
    """Distinct http(s) URLs from <a href> attributes, in document order."""
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a"):
        href = str(a.get("href") or "").strip()
        if href.startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            out.append(href)
    return out


def _merge_urls(base: list[str], extra: list[str]) -> list[str]:
    """Append URLs from `extra` to `base` preserving order, dedup'd."""
    seen = set(base)
    out = list(base)
    for u in extra:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
