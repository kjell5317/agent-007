"""kotx ingestion source.

Fed a batch of kotx task payloads (from the webhook or the reconciliation
poll) and yields one envelope per transition. For actionable states it
fetches the brief (TASK.md / REVIEW.md) so the envelope content carries the
document the agent needs for estimation and the inbox can display it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from app.db.schemas.raw_input import RawInputCreate
from app.services.input.base import IngestionSource, register_source
from app.services.input.kotx.normalize import (
    brief_doc_for,
    envelope_for_transition,
    sort_key,
)
from app.services.kotx import client as kotx_client

log = logging.getLogger(__name__)


@register_source("kotx")
class KotxSource(IngestionSource):
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads

    async def fetch(self) -> AsyncIterator[RawInputCreate]:
        for task in sorted(self.payloads, key=sort_key):
            doc: str | None = None
            doc_kind = brief_doc_for(task)
            if doc_kind and task.get("id") is not None:
                try:
                    doc = await kotx_client.fetch_doc(int(task["id"]), doc_kind)
                except Exception:  # noqa: BLE001 — the transition still counts without its brief
                    log.exception("kotx doc fetch failed · task=%s", task.get("id"))
            envelope = envelope_for_transition(task, doc)
            if envelope is not None:
                yield envelope
