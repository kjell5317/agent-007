"""tasks: scheduled_date nullable again (null == unscheduled)

Reverses the NOT NULL from 0013. A task that can't be placed before its due
date now clears its slot (`scheduled_date = NULL`); `open_unscheduled_due` and
the retry cron sweep pick it back up. server_default stays now() so a freshly
created row still gets a provisional slot before the planner assigns a real one.

Note: `revision` must fit alembic_version.version_num (varchar(32)).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_scheduled_date_nullable"
down_revision = "0017_task_kotx_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tasks",
        "scheduled_date",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.text("now()"),
        nullable=True,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE tasks SET scheduled_date = now() - interval '1 day' "
        "WHERE scheduled_date IS NULL"
    )
    op.alter_column(
        "tasks",
        "scheduled_date",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.text("now()"),
        nullable=False,
    )
