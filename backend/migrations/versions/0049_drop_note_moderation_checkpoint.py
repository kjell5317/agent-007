"""Drop the startup sweep's checkpoint; every note is moderated once on creation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0049_drop_note_moderation_checkpoint"
down_revision = "0048_handoff_research"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("note_moderation_checkpoint")


def downgrade() -> None:
    op.create_table(
        "note_moderation_checkpoint",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_note_id", UUID(as_uuid=True), nullable=True),
    )
    op.execute("INSERT INTO note_moderation_checkpoint (id) VALUES (1)")
