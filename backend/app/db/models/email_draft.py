import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EmailDraft(Base):
    """A reply draft 007 put in a mailbox. 007 never sends it; the poll notices
    when the user did."""

    __tablename__ = "email_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    raw_input_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_inputs.id", ondelete="SET NULL"), nullable=True
    )
    from_address: Mapped[str] = mapped_column(String(320))
    to: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    open_questions: Mapped[list] = mapped_column(JSON, default=list)
    provider_draft_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Gmail thread id; sent detection matches on it.
    provider_thread_ref: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    # generated | pushed | stale | sent | discarded
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
