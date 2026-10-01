"""Link tasks to source events and track moderated note provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0028_note_moderation_event_link"
down_revision = "0027_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("related_event_id", sa.String(256), nullable=True))
    op.add_column("tasks", sa.Column("related_event_calendar_id", sa.String(256), nullable=True))
    op.add_column(
        "tasks", sa.Column("related_event_due_derived", sa.Boolean(), nullable=False,
                           server_default=sa.false())
    )
    op.create_index(
        "ix_tasks_related_event", "tasks", ["related_event_calendar_id", "related_event_id"]
    )
    op.add_column(
        "notes", sa.Column("source_raw_input_ids", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column(
        "notes", sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.execute(
        "UPDATE notes SET source_raw_input_ids = json_build_array(source_raw_input_id::text) "
        "WHERE source_raw_input_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("notes", "needs_review")
    op.drop_column("notes", "source_raw_input_ids")
    op.drop_index("ix_tasks_related_event", table_name="tasks")
    op.drop_column("tasks", "related_event_calendar_id")
    op.drop_column("tasks", "related_event_due_derived")
    op.drop_column("tasks", "related_event_id")
