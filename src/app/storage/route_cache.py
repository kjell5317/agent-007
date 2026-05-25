from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.route_cache import RouteCache


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
