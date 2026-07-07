"""Dump the custom event labels defined on the configured Google calendar.

Custom labels (id, name, backgroundColor) live only in the Calendars API under
`labelProperties.eventLabels` — they aren't visible in the Calendar UI and
aren't returned by the fixed Colors palette. This prints them so their ids can
be wired into config/labels.toml.

Run where the app's Google token is reachable (i.e. on the deployed box):

    PYTHONPATH=backend python3 backend/scripts/dump_calendar_labels.py
"""

import asyncio
import json

import httpx

from app.auth.google_tokens import get_fresh_google_token
from app.config import get_settings
from app.db import SessionLocal
from app.labels import load_labels

_BASE = "https://www.googleapis.com/calendar/v3"


async def main() -> None:
    settings = get_settings()
    calendar_id = (settings.google_calendar_id or "primary").strip()

    with SessionLocal() as session:
        token = await get_fresh_google_token(session)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{_BASE}/calendars/{calendar_id}",
            headers={"Authorization": f"Bearer {token.access_token}"},
            # eventLabelVersion surfaces labelProperties; ignored where unsupported.
            params={"eventLabelVersion": "1"},
        )
        resp.raise_for_status()
        cal = resp.json()

    labels = (cal.get("labelProperties") or {}).get("eventLabels") or []
    print(f"\ncalendar: {calendar_id}  ({cal.get('summary')!r})")
    print(f"labels defined on calendar: {len(labels)}\n")

    if not labels:
        print("No custom event labels found on this calendar.")
        print("raw labelProperties:", cal.get("labelProperties"))
        return

    # Hint the likely config match by color: the existing TOML maps names to
    # colorIds, and Google returns a hex per label — no shared key, so this is
    # only a name/color eyeball aid, not an authoritative mapping.
    toml = load_labels()

    print(f"{'id':<40} {'name':<24} {'bgColor':<9}")
    print("-" * 75)
    for lab in labels:
        print(
            f"{lab.get('id', ''):<40} "
            f"{(lab.get('name') or '(unnamed)'):<24} "
            f"{(lab.get('backgroundColor') or ''):<9}"
        )

    if toml:
        print("\nconfig/labels.toml entries (name → current colorId):")
        for name, label in toml.items():
            print(f"  {name:<12} color={label.color or '-'}")

    print("\nfull labelProperties JSON:")
    print(json.dumps(cal.get("labelProperties"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
