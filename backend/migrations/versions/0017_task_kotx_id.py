"""tasks: kotx_task_id link column

Revision ID: 0017_task_kotx_id
Revises: 0016_geocode_cache
Create Date: 2026-07-03

One 007 task per kotx coding-agent task. Unique so transition matching by
kotx id is unambiguous; nullable because most tasks have no kotx run.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_task_kotx_id"
down_revision = "0016_geocode_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("kotx_task_id", sa.Integer(), nullable=True))
    op.create_unique_constraint("uq_tasks_kotx_task_id", "tasks", ["kotx_task_id"])


def downgrade() -> None:
    op.drop_constraint("uq_tasks_kotx_task_id", "tasks", type_="unique")
    op.drop_column("tasks", "kotx_task_id")
