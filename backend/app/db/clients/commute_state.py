"""The stored bike / user position (single row)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models.scheduling import CommuteStateRow
from app.services.commute.state import Position


def get(session: Session) -> Position:
    row = session.get(CommuteStateRow, 1)
    if row is None:
        return Position()
    return Position(
        bike_location=row.bike_location,
        bike_anchor=row.bike_anchor,
        bike_since=row.bike_since,
        user_location=row.user_location,
        user_anchor=row.user_anchor,
        user_since=row.user_since,
        applied_through=row.applied_through,
    )


def save(session: Session, position: Position) -> None:
    row = session.get(CommuteStateRow, 1)
    if row is None:
        row = CommuteStateRow(id=1)
        session.add(row)
    row.bike_location = position.bike_location
    row.bike_anchor = position.bike_anchor
    row.bike_since = position.bike_since
    row.user_location = position.user_location
    row.user_anchor = position.user_anchor
    row.user_since = position.user_since
    row.applied_through = position.applied_through
    session.flush()
