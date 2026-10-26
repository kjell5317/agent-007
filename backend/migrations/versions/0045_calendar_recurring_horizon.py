"""Drop cached instances of recurring series more than a year ahead: an
open-ended series was cached out to 2054. The cache now keeps only the next
year of a series (`calendar.cache.RECURRING_HORIZON`). Instance ids are the
series id plus `_<start>`; one-off events far ahead stay."""

from alembic import op

revision = "0045_calendar_recurring_horizon"
down_revision = "0044_raw_inputs_subject_trgm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM documents WHERE provider = 'calendar' "
        "AND starts_at > now() + interval '365 days' "
        "AND external_id ~ '_[0-9]{8}(T[0-9]{6}Z)?$'"
    )


def downgrade() -> None:
    # The next sync of each series brings the instances back within the horizon.
    pass
