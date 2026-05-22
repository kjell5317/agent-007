import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RawInput(Base):
    """Normalized envelope around any incoming message before agent processing.

    Every source (Gmail, Slack, manual, ...) maps into this shape so the agent
    and storage layers stay source-agnostic.
    """

    __tablename__ = "raw_inputs"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_raw_inputs_source_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source: Mapped[str] = mapped_column(String(64), index=True)
    external_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    content: Mapped[str] = mapped_column(Text)
    # TODO: store structured fields (sender, subject, channel, urls, ...) per source
    source_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # TODO: status enum (received | processing | processed | failed | skipped)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)

    # TODO: store the agent's raw decision payload for auditing / replay
    agent_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
