"""Mirror the calendar's custom event labels into the `labels` table.

Google owns the label set: they're created and colored in the Calendar UI and
read back from `calendars.get`. This module pulls them in so the agent, the API
and the task mirror can work off the local table; the user-owned description /
repo mapping on each row survives the refresh untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.clients import labels as labels_store
from app.db.models.label import Label
from app.services.calendar.client import authorized_client

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GoogleLabel:
    google_id: str
    name: str
    background_color: str


def parse_labels(calendar: dict) -> list[GoogleLabel]:
    """Extract the event labels from a `calendars.get` payload."""
    raw = (calendar.get("labelProperties") or {}).get("eventLabels") or []
    out: list[GoogleLabel] = []
    for item in raw:
        google_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not google_id or not name:
            continue
        out.append(
            GoogleLabel(
                google_id=google_id,
                name=name,
                background_color=str(item.get("backgroundColor") or "").strip(),
            )
        )
    return out


async def sync(session: Session, *, account_key: str | None = None) -> list[Label]:
    """Refresh the local mirror from Google and return the full catalog."""
    calendar_id = (get_settings().google_calendar_id or "primary").strip()
    client = await authorized_client(session, account_key)
    fetched = parse_labels(await client.get_calendar(calendar_id))

    for label in fetched:
        labels_store.upsert_from_google(
            session,
            google_id=label.google_id,
            name=label.name,
            background_color=label.background_color,
        )
    dropped = labels_store.prune_missing(session, [label.google_id for label in fetched])
    session.commit()

    log.info("label sync · calendar=%s fetched=%d dropped=%d", calendar_id, len(fetched), dropped)
    return labels_store.list_all(session)
