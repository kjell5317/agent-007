"""raw_inputs: allow 'event' status

Revision ID: 0010_raw_input_status_event
Revises: 0009_points_entries
Create Date: 2026-06-12
"""
from __future__ import annotations

from alembic import op

revision = "0010_raw_input_status_event"
down_revision = "0009_points_entries"
branch_labels = None
depends_on = None

_OLD = "status IN ('processing','not_task','duplicate','open','closed')"
_NEW = "status IN ('processing','not_task','duplicate','open','closed','event')"


def upgrade() -> None:
    op.drop_constraint("ck_raw_inputs_status", "raw_inputs", type_="check")
    op.create_check_constraint("ck_raw_inputs_status", "raw_inputs", _NEW)


def downgrade() -> None:
    op.drop_constraint("ck_raw_inputs_status", "raw_inputs", type_="check")
    op.create_check_constraint("ck_raw_inputs_status", "raw_inputs", _OLD)
