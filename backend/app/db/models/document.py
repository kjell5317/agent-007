import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EMBEDDING_DIM = 1536


class Document(Base):
    """External-reference corpus row (calendar, notion, drive, kotx, …).

    Distinct from `raw_inputs`: those are candidate *tasks* for the agent to
    decide on; documents are reference material the search layer retrieves
    over. A provider sync job upserts here on `(provider, external_id)`; stage-1
    suggest and stage-2 hybrid search read it. Empty until a sync job runs.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_documents_provider_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    provider: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(512))

    title: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `metadata` is reserved on the declarative Base, so the attribute is `meta`.
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    # Generated in the DB; read-only from the ORM's side.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', "
            "coalesce(title, '') || ' ' || coalesce(snippet, '') || ' ' || coalesce(content, ''))",
            persisted=True,
        ),
        nullable=True,
    )
