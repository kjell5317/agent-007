"""Notification ledger: what went out under which key, and whether it still shows."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_notification_ledger"
down_revision = "0050_drop_firm_deadline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_ledger",
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("tag", sa.String(512), nullable=False),
        sa.Column("fingerprint", sa.String(512), nullable=False, server_default=""),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_notification_ledger_kind", "notification_ledger", ["kind"])
    op.create_index("ix_notification_ledger_task_id", "notification_ledger", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_ledger_task_id", table_name="notification_ledger")
    op.drop_index("ix_notification_ledger_kind", table_name="notification_ledger")
    op.drop_table("notification_ledger")
