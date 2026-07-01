"""tasks: scheduled_date NOT NULL with a backfill

Revision ID: 0013_task_scheduled_date_not_null
Revises: 0012_task_scheduled_date
Create Date: 2026-07-01

Every task must carry a slot. Tasks that never got one (created before this
column existed, or that failed to schedule) are backfilled with a slot one day
in the past so the overdue-reschedule cron immediately re-plans them onto a
real future slot. New rows default to now() to keep the invariant on insert.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_task_scheduled_date_not_null"
down_revision = "0012_task_scheduled_date"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE tasks SET scheduled_date = now() - interval '1 day' "
        "WHERE scheduled_date IS NULL"
    )
    op.alter_column(
        "tasks",
        "scheduled_date",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "scheduled_date",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )
