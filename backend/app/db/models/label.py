from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Label(Base):
    """A Google Calendar event label, mirrored locally with our own metadata.

    Google owns `name` and `background_color` — they're refreshed on every sync
    and never edited here. `description` and `github_repo` are ours: the
    description is what the agent sees when picking a label (a label without
    one is hidden from the agent), and `github_repo` maps an `owner/repo` to
    the label its coding tasks get.
    """

    __tablename__ = "labels"

    google_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    name: Mapped[str] = mapped_column(String(128))
    background_color: Mapped[str] = mapped_column(String(16))

    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    github_repo: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
