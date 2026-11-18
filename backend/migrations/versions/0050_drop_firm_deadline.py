"""Drop the `firm` deadline kind: every firm deadline becomes hard, the new default."""

from alembic import op

revision = "0050_drop_firm_deadline"
down_revision = "0049_drop_note_mod_checkpoint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE tasks SET deadline_kind = 'hard' WHERE deadline_kind = 'firm'")
    for column in ("agent_value", "user_value"):
        op.execute(
            f"UPDATE task_overrides SET {column} = '\"hard\"'::jsonb "
            f"WHERE field = 'deadline_kind' AND {column} = '\"firm\"'::jsonb"
        )
    op.alter_column("tasks", "deadline_kind", server_default="hard")


def downgrade() -> None:
    op.alter_column("tasks", "deadline_kind", server_default="firm")
