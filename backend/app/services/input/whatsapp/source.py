"""WhatsApp ingestion source.

Push-driven, unlike Gmail/Slack: the Cloud API has no endpoint to poll for
received messages, so there is no `poll()` routine and WhatsApp is absent from
`app.services.input.poll._POLLERS`. Instead `POST /webhooks/whatsapp`
(`app.api.webhooks`) parses Meta's payload, seeds this source with the
resulting `ParsedMessage`s, and runs it through the shared `drain()` pipeline —
reusing the same persist + embed + agent + notify path as every other source.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.db.schemas.raw_input import RawInputCreate
from app.services.input.base import IngestionSource, register_source
from app.services.input.whatsapp.preprocess import ParsedMessage


@register_source("whatsapp")
class WhatsAppSource(IngestionSource):
    def __init__(self, *, messages: list[ParsedMessage]):
        self.messages = messages

    async def fetch(self) -> AsyncIterator[RawInputCreate]:
        for m in self.messages:
            yield RawInputCreate(
                source="whatsapp",
                external_id=m.message_id,
                content=m.body,
                source_metadata={
                    "account": m.phone_number_id,
                    "from": m.from_number,
                    "sender_name": m.sender_name,
                    "type": m.type,
                    "timestamp": m.timestamp,
                    "truncated": m.truncated,
                },
            )
