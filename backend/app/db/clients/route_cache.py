from __future__ import annotations

from sqlalchemy import func, select, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.route_cache import RouteCache


def lookup(
    session: Session,
    *,
    origin: str,
    destination: str,
    mode: str,
    hour_bucket: int,
) -> RouteCache | None:
    stmt = select(RouteCache).where(
        RouteCache.origin == origin,
        RouteCache.destination == destination,
        RouteCache.mode == mode,
        RouteCache.hour_bucket == hour_bucket,
    )
    return session.execute(stmt).scalar_one_or_none()


def location_suggestions(session: Session, *, query: str, limit: int = 3) -> list[str]:
    """Return cached origin/destination strings matching a location draft."""
    trimmed = query.strip()

    origin_stmt = select(RouteCache.origin.label("location")).where(RouteCache.origin != "")
    destination_stmt = select(RouteCache.destination.label("location")).where(
        RouteCache.destination != ""
    )
    if trimmed:
        pattern = f"%{trimmed}%"
        origin_stmt = origin_stmt.where(RouteCache.origin.ilike(pattern))
        destination_stmt = destination_stmt.where(RouteCache.destination.ilike(pattern))

    locations = union_all(origin_stmt, destination_stmt).subquery()
    location = locations.c.location
    stmt = (
        select(location)
        .group_by(location)
        .order_by(func.lower(location), location)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars().all())


def upsert(
    session: Session,
    *,
    origin: str,
    destination: str,
    mode: str,
    hour_bucket: int,
    duration_seconds: int,
    distance_meters: int | None,
) -> RouteCache:
    """Insert or refresh a cached route. Returns the persisted row.

    The unique key is `(origin, destination, mode, hour_bucket)`; on conflict
    we refresh the duration so a route that genuinely changed (a new transit
    line, a closed road) eventually settles to the new value rather than
    sticking on the first reading forever.
    """
    stmt = (
        insert(RouteCache)
        .values(
            origin=origin,
            destination=destination,
            mode=mode,
            hour_bucket=hour_bucket,
            duration_seconds=duration_seconds,
            distance_meters=distance_meters,
        )
        .on_conflict_do_update(
            constraint="uq_route_cache_lookup",
            set_={
                "duration_seconds": duration_seconds,
                "distance_meters": distance_meters,
            },
        )
        .returning(RouteCache)
    )
    row = session.execute(stmt).scalar_one()
    session.flush()
    return row
