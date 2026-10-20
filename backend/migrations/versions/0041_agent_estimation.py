"""The agent's raw duration estimate, kept beside the corrected `estimation`
so the duration model keeps training on raw estimates."""

import sqlalchemy as sa
from alembic import op

revision = "0041_agent_estimation"
down_revision = "0040_commute_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("agent_estimation", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "agent_estimation")
