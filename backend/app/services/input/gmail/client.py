"""Thin async wrapper around the Gmail REST API.

Only the calls the ingestion source needs:
  * `list_messages` (initial bootstrap with a `q=` filter)
  * `history_list`  (incremental sync from a stored `historyId`)
  * `get_message`   (fetch full payload for preprocessing)

Uses httpx directly rather than the official google-api-python-client to
avoid pulling in the full Google client stack for three endpoints.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailClient:
    """Authenticated Gmail API client for a single account.

    Caller is responsible for refreshing the access token before instantiation
    if it has expired (see `GoogleOAuthProvider.refresh`).
    """

    def __init__(self, access_token: str, *, timeout: float = 15.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def list_messages(
        self, query: str | None = None, max_results: int = 50
    ) -> AsyncIterator[str]:
        """Yield message ids matching `query` (Gmail search syntax)."""
        params: dict[str, Any] = {"maxResults": max_results}
        if query:
            params["q"] = query
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            while True:
                resp = await client.get(f"{_BASE}/messages", params=params)
                resp.raise_for_status()
                payload = resp.json()
                for m in payload.get("messages", []):
                    yield m["id"]
                token = payload.get("nextPageToken")
                if not token:
                    return
                params["pageToken"] = token

    async def history_list(self, start_history_id: str) -> AsyncIterator[dict]:
        """Yield history records since `start_history_id`.

        Each record has the shape `{id, messages, messagesAdded, ...}`. Caller
        filters to whatever event kinds it cares about (typically `messagesAdded`).
        """
        params: dict[str, Any] = {"startHistoryId": start_history_id}
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            while True:
                resp = await client.get(f"{_BASE}/history", params=params)
                if resp.status_code == 404:
                    # historyId too old to expand — caller must fall back to list_messages.
                    raise HistoryExpiredError(start_history_id)
                resp.raise_for_status()
                payload = resp.json()
                for h in payload.get("history", []):
                    yield h
                token = payload.get("nextPageToken")
                if not token:
                    return
                params["pageToken"] = token

    async def get_message(self, message_id: str) -> dict | None:
        """Fetch a single message in `format=full` for preprocessing.

        Returns `None` when Gmail no longer has the message (404 — typical when
        a history record references a message that's since been hard-deleted).
        Caller should skip such ids; raising would block the entire poll and
        prevent the history watermark from advancing past the broken record.
        """
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.get(
                f"{_BASE}/messages/{message_id}", params={"format": "full"}
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    async def get_profile_history_id(self) -> str:
        """Latest historyId for the mailbox — used to seed incremental sync."""
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            resp = await client.get(f"{_BASE}/profile")
            resp.raise_for_status()
            return resp.json()["historyId"]


class HistoryExpiredError(Exception):
    """Raised when `historyId` is older than Gmail's retention window."""
