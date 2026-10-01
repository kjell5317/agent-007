"""Persistent open counts for results in the search view."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SearchResultClick(Base):
    __tablename__ = "search_result_clicks"

    hit_type: Mapped[str] = mapped_column(Text, primary_key=True)
    hit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    hit: Mapped[dict] = mapped_column(JSONB, nullable=False)
    click_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_clicked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
