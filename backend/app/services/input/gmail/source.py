"""Gmail ingestion source.

Bridges the Google OAuth + Gmail API layer into the generic `IngestionSource`
contract. Per-account: instantiate with an `account_key` (the user's email
address, returned by `GoogleOAuthProvider.identify`).

Fetch strategy
--------------
Incremental sync via Gmail's `historyId`:

  1. Look up the stored watermark for this account.
  2. If none, bootstrap with `list_messages(query="...")` and advance to the
     mailbox's current `historyId`.
  3. Otherwise call `history_list(start_history_id=watermark)`, yielding any
     `messagesAdded`. On `HistoryExpiredError`, fall back to bootstrap.
  4. Persist the new `historyId` so the next run resumes cleanly.

Webhook support (Pub/Sub push) is left as a TODO — for personal use, polling
on a schedule is simpler and avoids Pub/Sub topic setup.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.services.input.base import IngestionSource, register_source
from app.services.input.gmail.client import GmailClient, HistoryExpiredError
from app.services.input.gmail.preprocess import preprocess_message
from app.db.schemas.raw_input import RawInputCreate

log = logging.getLogger(__name__)

# Initial-bootstrap filter. Tightened to inbox to keep the first
# sync small; widen once the agent's behaviour is trusted.
BOOTSTRAP_QUERY = "in:inbox newer_than:1d"

# Gmail label IDs to skip outright. SENT covers messages the user authored
# (incl. self-cc), DRAFT is unfinished, SPAM/TRASH are obvious. The history
# API surfaces all of these as `messagesAdded` events, so we filter here
# rather than at the search-query level (which only applies on bootstrap).
SKIP_LABELS = frozenset({"DRAFT", "SPAM", "TRASH", "SENT"})


@register_source("gmail")
class GmailSource(IngestionSource):
    def __init__(self, account_key: str, access_token: str, history_id: str | None = None):
        self.account_key = account_key
        self.client = GmailClient(access_token)
        self.history_id = history_id
        # The new high-watermark observed during this fetch, written back by
        # the caller into the OAuthToken row's `extra` field after a successful run.
        self.next_history_id: str | None = None

    async def fetch(self) -> AsyncIterator[RawInputCreate]:
        async for message_id in self._iter_new_message_ids():
            raw = await self.client.get_message(message_id)
            if raw is None:
                # History referenced a message that's gone (hard-deleted,
                # purged, etc). Skip it so the watermark can still advance.
                log.info("gmail · skip id=%s (404 from get_message)", message_id)
                continue

            labels = raw.get("labelIds") or ()
            skip = SKIP_LABELS.intersection(labels)
            if skip:
                log.debug("gmail · skip id=%s labels=%s", message_id, sorted(skip))
                continue

            result = preprocess_message(raw, account_email=self.account_key)

            # TODO: optional second-pass filter (sender allowlist, subject regex, ...)

            yield RawInputCreate(
                source="gmail",
                external_id=message_id,
                content=result.body,
                source_metadata={
                    "account": self.account_key,
                    "truncated": result.truncated,
                    **result.metadata,
                },
            )

        # After draining, capture the new watermark for the caller to persist.
        self.next_history_id = await self.client.get_profile_history_id()

    # --- internal -------------------------------------------------------------

    async def _iter_new_message_ids(self) -> AsyncIterator[str]:
        if self.history_id is None:
            log.info("gmail fetch · bootstrap query=%r", BOOTSTRAP_QUERY)
            async for mid in self.client.list_messages(query=BOOTSTRAP_QUERY):
                yield mid
            return

        log.info("gmail fetch · incremental from history_id=%s", self.history_id)
        try:
            async for record in self.client.history_list(self.history_id):
                for added in record.get("messagesAdded", []):
                    message = added.get("message", {})
                    mid = message.get("id")
                    if mid:
                        yield mid
        except HistoryExpiredError:
            # Watermark too old — re-bootstrap. Caller will overwrite history_id
            # via `next_history_id` after the new mailbox state is captured.
            log.warning(
                "gmail fetch · history_id=%s expired, falling back to bootstrap",
                self.history_id,
            )
            async for mid in self.client.list_messages(query=BOOTSTRAP_QUERY):
                yield mid
