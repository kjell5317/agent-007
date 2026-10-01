"""Link task groups and ordered subtasks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0030_task_subtasks"
down_revision = "0029_multilingual_lexical_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("parent_task_id", UUID(as_uuid=True), nullable=True))
    op.add_column("tasks", sa.Column("depends_on_task_id", UUID(as_uuid=True), nullable=True))
    op.add_column("tasks", sa.Column("is_container", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("tasks", sa.Column("due_date_derived", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_foreign_key("fk_tasks_parent", "tasks", "tasks", ["parent_task_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_tasks_dependency", "tasks", "tasks", ["depends_on_task_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.create_index("ix_tasks_depends_on_task_id", "tasks", ["depends_on_task_id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_depends_on_task_id", table_name="tasks")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_constraint("fk_tasks_dependency", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_parent", "tasks", type_="foreignkey")
    op.drop_column("tasks", "due_date_derived")
    op.drop_column("tasks", "is_container")
    op.drop_column("tasks", "depends_on_task_id")
    op.drop_column("tasks", "parent_task_id")
