"""labels: mirror of the Google Calendar event labels + local metadata

Replaces the hand-maintained `config/labels.toml` catalog. Google owns the
label set (name, background color); this table caches it so the agent and the
UI don't need a Calendar round-trip, and carries the two fields Google has no
place for: the description shown to the agent and an optional owner/repo used
to label coding tasks.

`google_id` is the natural key — the opaque label id from
`calendars.get?eventLabelVersion=1`, which is also what event writes send as
`eventLabelId`.

Revision ID: 0027_labels
Revises: 0026_chat_answers
Create Date: 2026-07-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_labels"
down_revision = "0026_chat_answers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "labels",
        sa.Column("google_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("background_color", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("github_repo", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Repo → label lookup runs on every kotx adoption; the mapping is unique.
    op.create_index(
        "ix_labels_github_repo",
        "labels",
        ["github_repo"],
        unique=True,
        postgresql_where=sa.text("github_repo IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_labels_github_repo", table_name="labels")
    op.drop_table("labels")
