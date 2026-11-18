import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SentNotification(Base):
    """The last push sent under a notification key, and whether it is still
    showing (`cleared_at` is null). Keeps a notification from going out twice
    and tells the reconciler which ones to clear."""

    __tablename__ = "notification_ledger"

    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    tag: Mapped[str] = mapped_column(String(512))
    fingerprint: Mapped[str] = mapped_column(String(512), default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
