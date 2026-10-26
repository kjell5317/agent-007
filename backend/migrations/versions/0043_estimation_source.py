"""Record which estimator produced a task's raw estimate, so the duration
model fits Jev's buckets apart from the LLM agent's guesses."""

import sqlalchemy as sa
from alembic import op

revision = "0043_estimation_source"
down_revision = "0042_route_cache_no_route"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("estimation_source", sa.String(8), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "estimation_source")
