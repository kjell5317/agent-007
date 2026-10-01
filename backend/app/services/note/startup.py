"""Resume historical note moderation in creation order after each restart."""

import asyncio
import logging

from sqlalchemy import text

from app.db import SessionLocal
from app.db.models.note import Note
from app.services.note.moderate import moderate_note

logger = logging.getLogger(__name__)


async def moderate_existing_notes() -> None:
    """Commit the note decision and cursor together; retry failures next start."""
    while True:
        try:
            with SessionLocal() as session:
                # A transaction-scoped lock prevents multiple workers from
                # moderating the same note at once.
                locked = session.execute(text(
                    "SELECT pg_try_advisory_xact_lock(7007, 30)"
                )).scalar()
                if not locked:
                    return
                checkpoint = session.execute(text(
                    "SELECT last_created_at, last_note_id "
                    "FROM note_moderation_checkpoint WHERE id = 1 FOR UPDATE"
                )).one()
                note = session.execute(text(
                    "SELECT id, created_at FROM notes WHERE "
                    "(CAST(:last_at AS timestamptz) IS NULL OR (created_at, id) > "
                    "(CAST(:last_at AS timestamptz), CAST(:last_id AS uuid))) "
                    "ORDER BY created_at, id LIMIT 1"
                ), {"last_at": checkpoint.last_created_at,
                    "last_id": checkpoint.last_note_id}).first()
                if note is None:
                    session.commit()
                    return
                row = session.get(Note, note.id)
                if row is not None:
                    await moderate_note(session, row.content, existing=row,
                                        raise_on_error=True)
                session.execute(text(
                    "UPDATE note_moderation_checkpoint SET "
                    "last_created_at = :created_at, last_note_id = :note_id WHERE id = 1"
                ), {"created_at": note.created_at, "note_id": note.id})
                session.commit()
            # Let request handling proceed between historical notes.
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Historical note moderation stopped; it will resume on next startup")
            return
