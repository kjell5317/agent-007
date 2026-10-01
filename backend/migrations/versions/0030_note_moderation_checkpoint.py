"""Track note freshness and resumable historical moderation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0030_note_moderation_checkpoint"
down_revision = "0029_multilingual_lexical_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notes", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE notes SET updated_at = created_at")
    op.alter_column("notes", "updated_at", nullable=False, server_default=sa.text("now()"))
    op.create_table(
        "note_moderation_checkpoint",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_note_id", UUID(as_uuid=True), nullable=True),
    )
    op.execute("INSERT INTO note_moderation_checkpoint (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("note_moderation_checkpoint")
    op.drop_column("notes", "updated_at")
