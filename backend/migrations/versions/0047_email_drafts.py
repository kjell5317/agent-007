"""Reply drafts 007 created in a mailbox for a task."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_email_drafts"
down_revision = "0046_task_handoffs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "raw_input_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_inputs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("to", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("open_questions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("provider_draft_id", sa.String(256), nullable=True),
        sa.Column("provider_thread_ref", sa.String(256), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_email_drafts_task_id", "email_drafts", ["task_id"])
    op.create_index("ix_email_drafts_provider_thread_ref", "email_drafts", ["provider_thread_ref"])


def downgrade() -> None:
    op.drop_index("ix_email_drafts_provider_thread_ref", table_name="email_drafts")
    op.drop_index("ix_email_drafts_task_id", table_name="email_drafts")
    op.drop_table("email_drafts")
