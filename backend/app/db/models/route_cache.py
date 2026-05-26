import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RouteCache(Base):
    """Cached Google Maps Distance Matrix lookups.

    A row is the duration of one (origin, destination, mode) trip departing in
    a given hour-of-week bucket. The bucket — `weekday * 24 + hour` — collapses
    rush-hour / off-peak variation into 168 slots per pairing, which gives us
    realistic public-transport times without re-querying Google for every event.

    Bike trips are mode-independent of the hour (cycling time doesn't vary with
    traffic on the Distance Matrix); we still bucket by hour so the schema stays
    uniform and so we can purge old rows by recency.
    """

    __tablename__ = "route_cache"
    __table_args__ = (
        UniqueConstraint(
            "origin", "destination", "mode", "hour_bucket",
            name="uq_route_cache_lookup",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    origin: Mapped[str] = mapped_column(String(512))
    destination: Mapped[str] = mapped_column(String(512))
    mode: Mapped[str] = mapped_column(String(16))
    hour_bucket: Mapped[int] = mapped_column(Integer)

    duration_seconds: Mapped[int] = mapped_column(Integer)
    distance_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
