from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.geocode_cache import GeocodeCache


def lookup(session: Session, *, address: str) -> tuple[float, float] | None:
    stmt = select(GeocodeCache).where(GeocodeCache.address == address)
    row = session.execute(stmt).scalar_one_or_none()
    return (row.lat, row.lon) if row is not None else None


def upsert(session: Session, *, address: str, lat: float, lon: float) -> None:
    stmt = (
        insert(GeocodeCache)
        .values(address=address, lat=lat, lon=lon)
        .on_conflict_do_update(
            constraint="uq_geocode_cache_address",
            set_={"lat": lat, "lon": lon},
        )
    )
    session.execute(stmt)
    session.flush()
