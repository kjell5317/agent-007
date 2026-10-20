"""Cache "no route" answers as a NULL duration, so unroutable pairs stop
costing a live Distance Matrix call on every solve."""

import sqlalchemy as sa
from alembic import op

revision = "0042_route_cache_no_route"
down_revision = "0041_agent_estimation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("route_cache", "duration_seconds", existing_type=sa.Integer(), nullable=True)
    # The first resolver marked no-route rows with a -1 sentinel.
    op.execute("UPDATE route_cache SET duration_seconds = NULL WHERE duration_seconds < 0")


def downgrade() -> None:
    op.execute("DELETE FROM route_cache WHERE duration_seconds IS NULL")
    op.alter_column("route_cache", "duration_seconds", existing_type=sa.Integer(), nullable=False)
