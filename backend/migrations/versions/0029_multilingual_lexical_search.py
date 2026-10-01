"""Add language-neutral lexical indexes for tasks and calendar documents."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision = "0029_multilingual_lexical_search"
down_revision = "0028_note_moderation_event_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column(
        "tsv_simple", TSVECTOR,
        sa.Computed("to_tsvector('simple', coalesce(title,'') || ' ' || "
                    "coalesce(description,'') || ' ' || coalesce(label,''))", persisted=True),
        nullable=True,
    ))
    op.create_index("ix_tasks_tsv_simple", "tasks", ["tsv_simple"], postgresql_using="gin")
    op.add_column("documents", sa.Column(
        "tsv_simple", TSVECTOR,
        sa.Computed("to_tsvector('simple', coalesce(title,'') || ' ' || "
                    "coalesce(snippet,'') || ' ' || coalesce(content,''))", persisted=True),
        nullable=True,
    ))
    op.create_index(
        "ix_documents_tsv_simple", "documents", ["tsv_simple"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_documents_tsv_simple", table_name="documents")
    op.drop_column("documents", "tsv_simple")
    op.drop_index("ix_tasks_tsv_simple", table_name="tasks")
    op.drop_column("tasks", "tsv_simple")
