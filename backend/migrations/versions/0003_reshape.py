"""reshape schema: status lives on raw_inputs; tasks linked by raw_inputs.task_id

Revision ID: 0003_reshape
Revises: 0002_raw_input_embedding
Create Date: 2026-05-24

Changes
-------
tasks:
  - drop: confidence, source_links, status, embedding, raw_input_id
  - rename: estimated_minutes -> estimation, due_at -> due_date
  - add:    link (single source URL)

raw_inputs:
  - add:    task_id FK -> tasks(id), nullable
  - status: values are now (processing | not_task | duplicate | open | closed)
            default 'processing'

feedback:
  - dropped (decisions live on raw_inputs.status + agent_trace)

Data migration
--------------
Existing rows are remapped before the new CHECK lands:

raw_inputs.status:
  received  -> processing
  processed -> open  if trace.outcome = task_created
            -> duplicate if trace.outcome = duplicate
            -> not_task  otherwise
  skipped   -> not_task  if trace.outcome in {not_a_task, not_task}
            -> duplicate if trace.outcome = duplicate
            -> not_task  otherwise

raw_inputs.task_id is back-filled from tasks.raw_input_id (reversed FK direction).
tasks.source_links[0] copied into tasks.link if present.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_reshape"
down_revision = "0002_raw_input_embedding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_feedback_task_id", table_name="feedback")
    op.drop_index("ix_feedback_raw_input_id", table_name="feedback")
    op.drop_table("feedback")

    # Add new columns first so we can back-fill before dropping the source columns.
    op.add_column("tasks", sa.Column("link", sa.String(1024), nullable=True))
    op.add_column(
        "raw_inputs",
        sa.Column(
            "task_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Reverse the FK direction: copy tasks.raw_input_id -> raw_inputs.task_id.
    op.execute(
        """
        UPDATE raw_inputs r
           SET task_id = t.id
          FROM tasks t
         WHERE t.raw_input_id = r.id
           AND t.raw_input_id IS NOT NULL
        """
    )

    # Copy first source_links entry into the new singular `link` column.
    op.execute(
        """
        UPDATE tasks
           SET link = (source_links->>0)
         WHERE source_links IS NOT NULL
           AND jsonb_typeof(source_links::jsonb) = 'array'
           AND jsonb_array_length(source_links::jsonb) > 0
        """
    )

    # Remap raw_inputs.status to the new enum BEFORE adding the CHECK constraint.
    op.execute(
        """
        UPDATE raw_inputs
           SET status = CASE
             WHEN status = 'received'  THEN 'processing'
             WHEN status = 'processed' AND (agent_trace->>'outcome') = 'task_created' THEN 'open'
             WHEN status = 'processed' AND (agent_trace->>'outcome') = 'duplicate'    THEN 'duplicate'
             WHEN status = 'processed' THEN 'not_task'
             WHEN status = 'skipped'   AND (agent_trace->>'outcome') = 'duplicate'    THEN 'duplicate'
             WHEN status = 'skipped'   THEN 'not_task'
             ELSE status
           END
        """
    )

    # Now drop the old structure.
    op.drop_index("ix_tasks_raw_input_id", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_constraint("tasks_raw_input_id_fkey", "tasks", type_="foreignkey")
    op.drop_column("tasks", "raw_input_id")
    op.drop_column("tasks", "embedding")
    op.drop_column("tasks", "status")
    op.drop_column("tasks", "source_links")
    op.drop_column("tasks", "confidence")
    op.alter_column("tasks", "estimated_minutes", new_column_name="estimation")
    op.alter_column("tasks", "due_at", new_column_name="due_date")

    op.drop_index("ix_raw_inputs_status", table_name="raw_inputs")
    op.alter_column(
        "raw_inputs",
        "status",
        server_default="processing",
        existing_type=sa.String(32),
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_raw_inputs_status",
        "raw_inputs",
        "status IN ('processing','not_task','duplicate','open','closed')",
    )
    op.create_index(
        "ix_raw_inputs_task_received",
        "raw_inputs",
        ["task_id", sa.text("received_at DESC")],
    )
    op.create_index("ix_raw_inputs_status", "raw_inputs", ["status"])
    op.execute(
        "CREATE INDEX ix_raw_inputs_thread "
        "ON raw_inputs ((source_metadata->>'thread_id'))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_raw_inputs_thread")
    op.drop_index("ix_raw_inputs_status", table_name="raw_inputs")
    op.drop_index("ix_raw_inputs_task_received", table_name="raw_inputs")
    op.drop_constraint("ck_raw_inputs_status", "raw_inputs", type_="check")
    op.alter_column(
        "raw_inputs",
        "status",
        server_default="received",
        existing_type=sa.String(32),
        existing_nullable=False,
    )
    op.drop_column("raw_inputs", "task_id")
    op.create_index("ix_raw_inputs_status", "raw_inputs", ["status"])

    op.drop_column("tasks", "link")
    op.alter_column("tasks", "due_date", new_column_name="due_at")
    op.alter_column("tasks", "estimation", new_column_name="estimated_minutes")
    op.add_column("tasks", sa.Column("confidence", sa.Float, nullable=True))
    op.add_column(
        "tasks",
        sa.Column("source_links", sa.JSON, nullable=False, server_default="[]"),
    )
    op.add_column(
        "tasks",
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
    )
    from pgvector.sqlalchemy import Vector
    op.add_column("tasks", sa.Column("embedding", Vector(1536), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "raw_input_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_inputs.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_raw_input_id", "tasks", ["raw_input_id"])

    op.create_table(
        "feedback",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tasks.id"),
            nullable=True,
        ),
        sa.Column(
            "raw_input_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_inputs.id"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("correction", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_feedback_task_id", "feedback", ["task_id"])
    op.create_index("ix_feedback_raw_input_id", "feedback", ["raw_input_id"])
