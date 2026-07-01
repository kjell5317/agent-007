"""tasks: due_date NOT NULL with a backfill

Revision ID: 0014_due_date_not_null
Revises: 0013_scheduled_date_not_null
Create Date: 2026-07-01

Every task needs a deadline — the planner places tasks in a `[now, due]`
window and can't schedule without one. Tasks that never got a due date are
backfilled a week out so they stay schedulable. New rows default to now() as
the NOT NULL safety net; `tasks.create()` supplies the real horizon default.

Note: `revision` must fit alembic_version.version_num (varchar(32)).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_due_date_not_null"
down_revision = "0013_scheduled_date_not_null"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE tasks SET due_date = now() + interval '7 days' "
        "WHERE due_date IS NULL"
    )
    op.alter_column(
        "tasks",
        "due_date",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "due_date",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )
