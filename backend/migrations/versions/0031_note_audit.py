"""Record note changes for moderation history.

This revision joins the two existing 0030 heads. Existing notes start with an
empty history; their earlier changes cannot be reconstructed from updated_at.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0031_note_audit"
down_revision = ("0030_note_moderation_checkpoint", "0030_task_subtasks")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "note_audit",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("note_id", UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("old_content", sa.Text, nullable=True),
        sa.Column("new_content", sa.Text, nullable=True),
        sa.Column("source_raw_input_id", UUID(as_uuid=True), nullable=True),
        sa.Column("needs_review", sa.Boolean, nullable=True),
    )
    op.create_index("ix_note_audit_note_time", "note_audit", ["note_id", "occurred_at"])
    op.execute("""
        CREATE FUNCTION record_note_audit() RETURNS trigger AS $$
        DECLARE
          change_action text;
          change_actor text := coalesce(nullif(current_setting('app.note_actor', true), ''), 'automated');
        BEGIN
          IF TG_OP = 'INSERT' THEN
            change_action := 'created';
            INSERT INTO note_audit
              (note_id, action, actor, old_content, new_content, source_raw_input_id, needs_review)
            VALUES
              (NEW.id, change_action, change_actor, NULL, NEW.content, NEW.source_raw_input_id, NEW.needs_review);
            RETURN NEW;
          ELSIF TG_OP = 'DELETE' THEN
            -- Deleting a note also removes its text from the audit history.
            UPDATE note_audit SET old_content = NULL, new_content = NULL
            WHERE note_id = OLD.id;
            INSERT INTO note_audit
              (note_id, action, actor, old_content, new_content, source_raw_input_id, needs_review)
            VALUES
              (OLD.id, 'deleted', change_actor, NULL, NULL, OLD.source_raw_input_id, OLD.needs_review);
            RETURN OLD;
          END IF;

          IF NEW.content IS DISTINCT FROM OLD.content THEN
            change_action := 'content_updated';
          ELSIF NEW.source_raw_input_ids::text IS DISTINCT FROM OLD.source_raw_input_ids::text
             OR NEW.source_raw_input_id IS DISTINCT FROM OLD.source_raw_input_id THEN
            change_action := 'source_linked';
          ELSIF NEW.needs_review IS DISTINCT FROM OLD.needs_review THEN
            change_action := 'review_changed';
          ELSE
            RETURN NEW;
          END IF;
          INSERT INTO note_audit
            (note_id, action, actor, old_content, new_content, source_raw_input_id, needs_review)
          VALUES
            (NEW.id, change_action, change_actor,
             CASE WHEN change_action = 'content_updated' THEN OLD.content END,
             CASE WHEN change_action = 'content_updated' THEN NEW.content END,
             NEW.source_raw_input_id, NEW.needs_review);
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_note_audit
        AFTER INSERT OR UPDATE OR DELETE ON notes
        FOR EACH ROW EXECUTE FUNCTION record_note_audit()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_note_audit ON notes")
    op.execute("DROP FUNCTION record_note_audit()")
    op.drop_index("ix_note_audit_note_time", table_name="note_audit")
    op.drop_table("note_audit")
