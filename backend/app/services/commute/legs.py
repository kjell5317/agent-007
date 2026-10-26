"""A commute leg as written to the calendar."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

HOME = "home"

# A leg neither bike nor transit can route is still written, as a visible
# "no route" placeholder in this mode, so the user gets time to travel somehow.
FAILED_MODE = "failed"


@dataclass(frozen=True)
class PlannedLeg:
    origin_anchor: str
    dest_anchor: str
    origin: str
    destination: str
    mode: str
    depart: datetime
    arrive: datetime
    reason: str | None = None
    note: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.origin_anchor, self.dest_anchor


def _norm(location: str) -> str:
    return " ".join(location.lower().split())
