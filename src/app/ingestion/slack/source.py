"""Slack ingestion source.

Per-account: one Slack workspace + user. Authed via `xoxp-` user token issued
by the Slack OAuth flow (see `app.auth.slack`).

Fetch strategy
--------------
Per-conversation watermarks (latest message `ts` we processed) stored in
`oauth_tokens.extra.channels = {channel_id: latest_ts}`:

  1. List the user's conversations via `users.conversations`.
  2. For each: fetch `conversations.history` with `oldest = latest_ts` if we
     have one, else with `oldest = now - SLACK_BOOTSTRAP_DAYS`.
  3. Skip the user's own messages and bot/system messages.
  4. Yield envelopes; track the new high-watermark per channel.

The caller persists `next_watermarks` into `oauth_tokens.extra.channels`
after a successful run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from app.config import get_settings
from app.ingestion.base import IngestionSource, register_source
from app.ingestion.slack.client import SlackAPIError, SlackClient
from app.ingestion.slack.preprocess import preprocess_message
from app.schemas.raw_input import RawInputCreate

log = logging.getLogger(__name__)

# Slack `subtype` values to skip. Regular human messages have no subtype.
# Thread broadcasts and replies (no subtype) are kept.
SKIP_SUBTYPES = frozenset(
    {
        "channel_join", "channel_leave", "channel_topic", "channel_purpose",
        "channel_name", "channel_archive", "channel_unarchive",
        "group_join", "group_leave", "group_topic", "group_purpose",
        "group_name", "group_archive", "group_unarchive",
        "bot_message", "bot_add", "bot_remove",
        "pinned_item", "unpinned_item",
        "reminder_add", "file_comment",
    }
)


@register_source("slack")
class SlackSource(IngestionSource):
    def __init__(
        self,
        *,
        account_key: str,
        access_token: str,
        authed_user_id: str | None,
        watermarks: dict[str, str] | None = None,
    ):
        self.account_key = account_key
        self.authed_user_id = authed_user_id
        self.client = SlackClient(access_token)
        self.watermarks = dict(watermarks or {})
        # Populated during fetch(); caller persists after a successful drain.
        self.next_watermarks: dict[str, str] = dict(self.watermarks)
        self._user_cache: dict[str, str] = {}

    async def fetch(self) -> AsyncIterator[RawInputCreate]:
        bootstrap_oldest = _bootstrap_oldest()
        log.info(
            "slack fetch · account=%s bootstrap_oldest=%s known_channels=%d",
            self.account_key, bootstrap_oldest, len(self.watermarks),
        )

        async for conv in self.client.users_conversations():
            channel_id = conv["id"]
            channel_name = conv.get("name") or _dm_label(conv)
            is_dm = bool(conv.get("is_im"))
            oldest = self.watermarks.get(channel_id) or bootstrap_oldest
            is_bootstrap = channel_id not in self.watermarks
            log.debug(
                "slack channel · %s (%s) oldest=%s mode=%s",
                channel_name, channel_id, oldest,
                "bootstrap" if is_bootstrap else "incremental",
            )

            new_max = self.watermarks.get(channel_id) or oldest
            yielded_for_channel = 0

            try:
                async for msg in self.client.conversations_history(channel_id, oldest=oldest):
                    ts = msg.get("ts")
                    if ts is None:
                        continue
                    if ts > new_max:
                        new_max = ts

                    if self._should_skip(msg):
                        continue

                    user_id = msg.get("user")
                    if user_id and user_id not in self._user_cache:
                        info = await self.client.users_info(user_id)
                        if info:
                            self._user_cache[user_id] = (
                                info.get("profile", {}).get("display_name")
                                or info.get("profile", {}).get("real_name")
                                or info.get("name")
                                or user_id
                            )

                    result = preprocess_message(
                        msg,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        user_names=self._user_cache,
                        authed_user_id=self.authed_user_id,
                        is_dm=is_dm,
                    )

                    if not result.body.strip():
                        continue

                    yield RawInputCreate(
                        source="slack",
                        external_id=f"{channel_id}:{ts}",
                        content=result.body,
                        source_metadata={
                            "account": self.account_key,
                            "truncated": result.truncated,
                            **result.metadata,
                        },
                    )
                    yielded_for_channel += 1
            except SlackAPIError as exc:
                # not_in_channel, missing_scope, etc. Skip and keep going —
                # don't burn the watermark on channels we can't read.
                log.info(
                    "slack channel skipped · %s (%s): %s",
                    channel_name, channel_id, exc,
                )
                continue

            if yielded_for_channel:
                log.info(
                    "slack channel done · %s (%s) yielded=%d new_watermark=%s",
                    channel_name, channel_id, yielded_for_channel, new_max,
                )
            self.next_watermarks[channel_id] = new_max

    def _should_skip(self, msg: dict) -> bool:
        if msg.get("subtype") in SKIP_SUBTYPES:
            return True
        if msg.get("bot_id"):
            return True
        # Own messages — same idea as Gmail's SENT filter.
        if self.authed_user_id and msg.get("user") == self.authed_user_id:
            return True
        return False


def _bootstrap_oldest() -> str:
    days = get_settings().slack_bootstrap_days
    return f"{time.time() - days * 86400:.6f}"


def _dm_label(conv: dict) -> str | None:
    """A best-effort label for DMs and MPDMs, which have no `name`."""
    if conv.get("is_im"):
        return f"DM:{conv.get('user', '?')}"
    if conv.get("is_mpim"):
        return conv.get("purpose", {}).get("value") or "(group DM)"
    return None
