"""Align existing parent deadlines with their last subtask."""

from alembic import op

revision = "0032_parent_subtask_deadlines"
down_revision = "0031_note_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        WITH last_child AS (
            SELECT DISTINCT ON (parent_task_id)
                parent_task_id, due_date
            FROM tasks
            WHERE parent_task_id IS NOT NULL
            ORDER BY parent_task_id, created_at DESC, id DESC
        )
        UPDATE tasks AS parent
        SET due_date = last_child.due_date
        FROM last_child
        WHERE parent.id = last_child.parent_task_id
          AND parent.is_container = true
          AND parent.due_date IS DISTINCT FROM last_child.due_date
    """)


def downgrade() -> None:
    # The previous parent deadlines cannot be reconstructed.
    pass
