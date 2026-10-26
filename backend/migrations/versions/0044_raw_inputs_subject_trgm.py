"""Trigram index on email subjects, so the Enter search's typo-tolerant
ranking can match messages the way it matches task and document titles."""

from alembic import op

revision = "0044_raw_inputs_subject_trgm"
down_revision = "0043_estimation_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_raw_inputs_subject_trgm ON raw_inputs "
        "USING gin ((source_metadata->>'subject') gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_raw_inputs_subject_trgm")
