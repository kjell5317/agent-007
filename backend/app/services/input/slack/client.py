"""Thin async wrapper around the Slack Web API.

Only the methods the ingestion source needs:
  * `users_conversations` — list channels/DMs/groups the user is in
  * `conversations_history` — fetch messages since a `ts` watermark
  * `users_info` — resolve user IDs to display names (cached by the source)

Uses httpx directly. Slack endpoints accept either form-encoded body or
query params; we use params for GETs and form data for POSTs to match
official examples.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

_BASE = "https://slack.com/api"

# Slack occasionally stalls a response past the read timeout or answers
# 429/5xx under load. Retry those so one slow request doesn't abort a
# whole poll cycle.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3


class SlackAPIError(Exception):
    """Raised when a Slack API response has `ok: false`."""


class SlackClient:
    def __init__(self, access_token: str, *, timeout: float = 15.0):
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self._timeout = timeout

    async def _get(
        self, client: httpx.AsyncClient, method: str, params: dict[str, Any]
    ) -> dict:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await client.get(f"{_BASE}/{method}", params=params)
            except httpx.TransportError:
                if attempt == _MAX_ATTEMPTS:
                    raise
                await asyncio.sleep(2 ** (attempt - 1))
                continue
            if resp.status_code in _RETRY_STATUS and attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(float(resp.headers.get("retry-after", 2 ** (attempt - 1))))
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")

    async def users_conversations(
        self, *, types: str = "public_channel,private_channel,im,mpim"
    ) -> AsyncIterator[dict]:
        """Yield conversations the authed user is a member of."""
        params: dict[str, Any] = {"types": types, "limit": 200, "exclude_archived": True}
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            while True:
                payload = await self._get(client, "users.conversations", params)
                if not payload.get("ok"):
                    raise SlackAPIError(payload.get("error", "unknown"))
                for c in payload.get("channels", []):
                    yield c
                cursor = (payload.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    return
                params["cursor"] = cursor

    async def conversations_history(
        self, channel: str, *, oldest: str | None = None
    ) -> AsyncIterator[dict]:
        """Yield messages in `channel` newer than `oldest` (Slack ts string)."""
        params: dict[str, Any] = {"channel": channel, "limit": 200}
        if oldest:
            params["oldest"] = oldest
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            while True:
                payload = await self._get(client, "conversations.history", params)
                if not payload.get("ok"):
                    # Common errors here: not_in_channel (user isn't a member),
                    # missing_scope. Surface so the caller can skip the channel.
                    raise SlackAPIError(payload.get("error", "unknown"))
                for m in payload.get("messages", []):
                    yield m
                if not payload.get("has_more"):
                    return
                cursor = (payload.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    return
                params["cursor"] = cursor

    async def get_permalink(self, channel: str, message_ts: str) -> str | None:
        """Canonical archive URL for a message via chat.getPermalink.

        Best-effort: returns None on any failure (deleted message, missing
        scope, transient error) so a missing permalink never drops the message.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
                payload = await self._get(
                    client,
                    "chat.getPermalink",
                    {"channel": channel, "message_ts": message_ts},
                )
        except httpx.HTTPError:
            return None
        if not payload.get("ok"):
            return None
        return payload.get("permalink")

    async def users_info(self, user_id: str) -> dict | None:
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            payload = await self._get(client, "users.info", {"user": user_id})
            if not payload.get("ok"):
                return None
            return payload.get("user")
