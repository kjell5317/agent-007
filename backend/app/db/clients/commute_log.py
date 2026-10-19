"""Commute mode flips: pins for upcoming legs and training rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.scheduling import CommuteFlip


def add(
    session: Session,
    *,
    origin_anchor: str,
    dest_anchor: str,
    origin: str | None,
    destination: str | None,
    depart: datetime,
    arrive: datetime | None,
    planned_mode: str,
    flipped_mode: str,
    past: bool,
    features: dict | None = None,
) -> CommuteFlip:
    row = CommuteFlip(
        origin_anchor=origin_anchor,
        dest_anchor=dest_anchor,
        origin=origin,
        destination=destination,
        depart=depart,
        arrive=arrive,
        planned_mode=planned_mode,
        flipped_mode=flipped_mode,
        past=past,
        features=features,
    )
    session.add(row)
    session.flush()
    return row


def active_pins(session: Session, now: datetime) -> dict[tuple[str, str], str]:
    """`{(origin_anchor, dest_anchor): mode}` for legs flipped ahead of time
    that haven't departed yet — the latest flip per leg wins."""
    rows = session.scalars(
        select(CommuteFlip)
        .where(CommuteFlip.past.is_(False), CommuteFlip.depart > now)
        .order_by(CommuteFlip.created_at)
    )
    pins: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row.origin_anchor, row.dest_anchor)
        # Re-insert so a later flip of the same leg also wins on order.
        pins.pop(key, None)
        pins[key] = row.flipped_mode
    return pins


def since(session: Session, start: datetime) -> list[CommuteFlip]:
    return list(session.scalars(
        select(CommuteFlip).where(CommuteFlip.created_at >= start).order_by(CommuteFlip.created_at)
    ))
