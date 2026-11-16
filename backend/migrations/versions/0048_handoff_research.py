"""Hand-offs: web research and personal-data lookup become one `research`
kind that reads links itself (no `url`), and every agent run keeps its trace."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_handoff_research"
down_revision = "0047_email_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE task_handoffs SET kind = 'research' "
        "WHERE kind IN ('web_research', 'own_data_lookup')"
    )
    op.drop_column("task_handoffs", "url")
    op.add_column("task_handoffs", sa.Column("trace", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_handoffs", "trace")
    op.add_column("task_handoffs", sa.Column("url", sa.Text(), nullable=True))
    op.execute("UPDATE task_handoffs SET kind = 'own_data_lookup' WHERE kind = 'research'")
