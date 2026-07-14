"""Note endpoints — audit and curate the agent's long-term memory.

  * GET    /notes            — list notes, newest first
  * PATCH  /notes/{note_id}  — edit a note's content (re-embeds it)
  * DELETE /notes/{note_id}  — delete a note

Notes are extracted by the agent flows (see `app.agent.tools.notes_lookup`);
this surface exists so the user can review and correct that memory by hand.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.clients import notes as notes_store
from app.db.schemas.note import NoteRead, NoteUpdate
from app.services.input.embedding import embed

router = APIRouter(prefix="/notes", tags=["notes"])


@router.get("", response_model=list[NoteRead])
async def list_notes(
    limit: int = Query(500, le=500),
    session: Session = Depends(get_session),
) -> list[NoteRead]:
    return [NoteRead.from_item(item) for item in notes_store.list_all(session, limit=limit)]


@router.patch("/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: uuid.UUID, payload: NoteUpdate, session: Session = Depends(get_session)
) -> NoteRead:
    content = payload.content.strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Note content cannot be empty")

    # Re-embed so future search_notes retrieval matches the edited text, not
    # the stale vector.
    embedding = await embed(content)
    if not notes_store.update(session, note_id, content=content, embedding=embedding):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    session.commit()

    item = notes_store.get_item(session, note_id)
    return NoteRead.from_item(item)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID, session: Session = Depends(get_session)
) -> None:
    if not notes_store.delete(session, note_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    session.commit()
