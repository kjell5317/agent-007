"""Give tasks stable, memorable public identifiers."""

import secrets
import string

import sqlalchemy as sa
from alembic import op

revision = "0036_task_public_id"
down_revision = "0035_subtask_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("public_id", sa.String(32), nullable=True))
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT id, parent_task_id FROM tasks ORDER BY created_at, id"
    )).all()
    alphabet = string.ascii_uppercase
    used: set[str] = set()
    root_codes: dict[object, str] = {}
    recent: list[str] = []
    for task_id, parent_id in rows:
        if parent_id is not None:
            continue
        candidates = {"".join(secrets.choice(alphabet) for _ in range(3)) for _ in range(96)} - used
        if not candidates:
            candidates = {f"{a}{b}{c}" for a in alphabet for b in alphabet for c in alphabet} - used
        code = max(candidates, key=lambda value: (
            min((sum(a != b for a, b in zip(value, old)) for old in recent), default=3),
            secrets.randbelow(1000),
        ))
        root_codes[task_id] = code
        used.add(code)
        recent = ([code] + recent)[:30]
        connection.execute(sa.text("UPDATE tasks SET public_id = :code WHERE id = :id"), {"code": code, "id": task_id})
    by_parent: dict[object, int] = {}
    for task_id, parent_id in rows:
        if parent_id is None:
            continue
        by_parent[parent_id] = by_parent.get(parent_id, 0) + 1
        code = f"{root_codes[parent_id]}{by_parent[parent_id]}"
        connection.execute(sa.text("UPDATE tasks SET public_id = :code WHERE id = :id"), {"code": code, "id": task_id})
    op.alter_column("tasks", "public_id", nullable=False)
    op.create_index("ix_tasks_public_id", "tasks", ["public_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tasks_public_id", table_name="tasks")
    op.drop_column("tasks", "public_id")
