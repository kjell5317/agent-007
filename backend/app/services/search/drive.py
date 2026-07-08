"""Google Drive federated search for chat/stage-2 retrieval.

A live `files.list` full-text query, run in parallel with the local hybrid
search and merged by the chat runner. Federated (not mirrored): Drive is too
big to index, and its content is reference material, not task input.

Best-effort by contract — no Google connection, an expired grant, a timeout or
any API error returns `[]` so the answer never blocks on Drive.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.auth.google_tokens import GoogleTokenError, get_fresh_google_token
from app.db.schemas.search import SearchHit
from app.services.search.extract import extract_text

log = logging.getLogger(__name__)

_BASE = "https://www.googleapis.com/drive/v3/files"
_FIELDS = "files(id,name,mimeType,modifiedTime,webViewLink)"

# Search surfaces documents a person actually reads — Docs/Sheets/Slides, PDFs,
# and Office files. An allowlist (rather than a code-extension blocklist) keeps
# out source code, Apps Script, images, archives, and other binaries by default.
_USEFUL_MIME_TYPES = (
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/rtf",
    "text/markdown",
)
_MIME_CLAUSE = "(" + " or ".join(f"mimeType = '{m}'" for m in _USEFUL_MIME_TYPES) + ")"


class DriveClient:
    """Authenticated Drive API client for a single account (mirrors GmailClient)."""

    def __init__(self, access_token: str, *, timeout: float = 5.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def search(
        self, query: str, *, limit: int, after: str | None = None, before: str | None = None
    ) -> list[dict]:
        clauses = [f"fullText contains '{_escape(query)}'", "trashed = false", _MIME_CLAUSE]
        if after:
            clauses.append(f"modifiedTime >= '{_rfc3339(after)}'")
        if before:
            clauses.append(f"modifiedTime < '{_rfc3339(before)}'")
        params = {
            "q": " and ".join(clauses),
            "fields": _FIELDS,
            "pageSize": limit,
            "orderBy": "modifiedTime desc",
            "spaces": "drive",
            "corpora": "user",
        }
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.get(_BASE, params=params)
            resp.raise_for_status()
            return resp.json().get("files", [])

    async def file_text(self, file_id: str, *, max_chars: int) -> str:
        """Plain-text content of a Drive file. Google-native docs are exported
        (Docs/Slides → text, Sheets → CSV); text/* files are read directly;
        PDFs and Office (docx/pptx/xlsx) are extracted from their bytes; images
        and other binaries can't be read."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            meta = await client.get(
                f"{_BASE}/{file_id}", params={"fields": "id,name,mimeType,webViewLink"}
            )
            meta.raise_for_status()
            info = meta.json()
            mime = info.get("mimeType") or ""
            name = info.get("name") or file_id
            link = info.get("webViewLink") or ""

            if mime.startswith("application/vnd.google-apps."):
                export_mime = "text/csv" if "spreadsheet" in mime else "text/plain"
                resp = await client.get(
                    f"{_BASE}/{file_id}/export", params={"mimeType": export_mime}
                )
                resp.raise_for_status()
                return _clip(name, resp.text, max_chars)

            if mime.startswith("text/") or mime in ("application/json", "application/xml"):
                resp = await client.get(f"{_BASE}/{file_id}", params={"alt": "media"})
                resp.raise_for_status()
                return _clip(name, resp.text, max_chars)

            # Rich binary (PDF / Office): download the bytes and extract locally.
            resp = await client.get(f"{_BASE}/{file_id}", params={"alt": "media"})
            resp.raise_for_status()
            extracted = extract_text(mime, resp.content, max_chars=max_chars)
            if extracted:
                return f"'{name}':\n{extracted}"
            return f"'{name}' is a {mime} file; text can't be extracted. Open it: {link}"


def _clip(name: str, body: str, max_chars: int) -> str:
    clipped = body[:max_chars]
    suffix = "\n…(truncated)" if len(body) > max_chars else ""
    return f"'{name}':\n{clipped}{suffix}"


def _escape(query: str) -> str:
    """Escape a term for a Drive `q` string literal (single-quoted)."""
    return query.replace("\\", "\\\\").replace("'", "\\'")


def _rfc3339(date: str) -> str:
    """A `YYYY-MM-DD` filter boundary → the RFC 3339 timestamp Drive expects."""
    return date if "T" in date else f"{date}T00:00:00"


async def search_drive(
    session: Session,
    query: str,
    *,
    k: int,
    timeout: float,
    after: str | None = None,
    before: str | None = None,
) -> list[SearchHit]:
    query = (query or "").strip()
    if not query:
        return []
    try:
        return await asyncio.wait_for(_search(session, query, k, after, before), timeout=timeout)
    except (asyncio.TimeoutError, GoogleTokenError, httpx.HTTPError) as exc:
        log.info("drive search skipped · %s: %s", type(exc).__name__, exc)
        return []


async def _search(
    session: Session, query: str, k: int, after: str | None, before: str | None
) -> list[SearchHit]:
    token = await get_fresh_google_token(session)
    files = await DriveClient(token.access_token).search(query, limit=k, after=after, before=before)
    return [_to_hit(f) for f in files]


async def get_drive_file(session: Session, file_id: str, *, max_chars: int) -> str:
    """Text content of one Drive file for the agent. Friendly message (never an
    exception) on a bad id, missing grant, or unextractable type."""
    file_id = (file_id or "").strip()
    if not file_id:
        return "get_drive_file: a `file_id` is required."
    try:
        token = await get_fresh_google_token(session)
        return await DriveClient(token.access_token).file_text(file_id, max_chars=max_chars)
    except (GoogleTokenError, httpx.HTTPError) as exc:
        log.info("get_drive_file failed · %s: %s", type(exc).__name__, exc)
        return f"get_drive_file: couldn't read that file ({type(exc).__name__})."


def _to_hit(f: dict) -> SearchHit:
    return SearchHit(
        type="drive",
        id=str(f.get("id") or ""),
        title=f.get("name") or "(untitled)",
        snippet=_mime_label(f.get("mimeType")),
        url=f.get("webViewLink"),
        source="drive",
        status="drive",
        ts=_parse_ts(f.get("modifiedTime")),
        score=0.0,
    )


def _mime_label(mime: str | None) -> str | None:
    if not mime:
        return None
    tail = mime.rsplit(".", 1)[-1]
    return {
        "document": "Google Doc",
        "spreadsheet": "Google Sheet",
        "presentation": "Google Slides",
        "folder": "Folder",
    }.get(tail, tail)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
