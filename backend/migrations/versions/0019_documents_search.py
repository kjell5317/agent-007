"""documents: external-reference search index + FTS/trigram search indexes

Foundation for staged hybrid search (docs/search-plan.md). Adds:

  * `documents` — general external-reference index (calendar, notion, drive,
    kotx, …). Empty until a provider sync job populates it; stage-1 suggest
    UNION-queries it alongside tasks/notes/raw_inputs from day one.
  * pg_trgm extension + trigram GIN indexes on the titles suggest matches
    against (`documents.title`, `tasks.title`, `notes.content`) for prefix /
    typo matching.
  * a generated `tsv` tsvector column on documents with its own GIN index —
    documents can carry long `content`, so unlike the small owned tables it
    gets a materialized FTS index rather than an inline `to_tsvector`.

Revision ID: 0019_documents_search
Revises: 0018_scheduled_date_nullable
Create Date: 2026-07-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID

revision = "0019_documents_search"
down_revision = "0018_scheduled_date_nullable"
branch_labels = None
depends_on = None

_TSV_EXPR = (
    "to_tsvector('english', "
    "coalesce(title, '') || ' ' || coalesce(snippet, '') || ' ' || coalesce(content, ''))"
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("snippet", sa.Text, nullable=True),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("metadata", JSONB, nullable=False, server_default="{}"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("tsv", TSVECTOR, sa.Computed(_TSV_EXPR, persisted=True)),
        sa.UniqueConstraint("provider", "external_id", name="uq_documents_provider_external_id"),
    )
    op.create_index("ix_documents_tsv", "documents", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_documents_title_trgm",
        "documents",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    # Real interval columns for calendar range queries (stage 2 + scheduling).
    op.create_index("ix_documents_provider_starts_at", "documents", ["provider", "starts_at"])

    op.create_index(
        "ix_tasks_title_trgm",
        "tasks",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_notes_content_trgm",
        "notes",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_notes_content_trgm", table_name="notes")
    op.drop_index("ix_tasks_title_trgm", table_name="tasks")
    op.drop_index("ix_documents_provider_starts_at", table_name="documents")
    op.drop_index("ix_documents_title_trgm", table_name="documents")
    op.drop_index("ix_documents_tsv", table_name="documents")
    op.drop_table("documents")
