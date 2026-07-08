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

log = logging.getLogger(__name__)

_BASE = "https://www.googleapis.com/drive/v3/files"
_FIELDS = "files(id,name,mimeType,modifiedTime,webViewLink)"


class DriveClient:
    """Authenticated Drive API client for a single account (mirrors GmailClient)."""

    def __init__(self, access_token: str, *, timeout: float = 5.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def search(self, query: str, *, limit: int) -> list[dict]:
        params = {
            "q": f"fullText contains '{_escape(query)}' and trashed = false",
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


def _escape(query: str) -> str:
    """Escape a term for a Drive `q` string literal (single-quoted)."""
    return query.replace("\\", "\\\\").replace("'", "\\'")


async def search_drive(
    session: Session, query: str, *, k: int, timeout: float
) -> list[SearchHit]:
    query = (query or "").strip()
    if not query:
        return []
    try:
        return await asyncio.wait_for(_search(session, query, k), timeout=timeout)
    except (asyncio.TimeoutError, GoogleTokenError, httpx.HTTPError) as exc:
        log.info("drive search skipped · %s: %s", type(exc).__name__, exc)
        return []


async def _search(session: Session, query: str, k: int) -> list[SearchHit]:
    token = await get_fresh_google_token(session)
    files = await DriveClient(token.access_token).search(query, limit=k)
    return [_to_hit(f) for f in files]


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
