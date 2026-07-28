"""Label endpoints — the catalog the agent picks from and the UI renders.

  * GET   /labels                  — the local mirror, no Calendar round-trip
  * POST  /labels/sync             — refresh the mirror from Google, then list
  * PATCH /labels/{google_id}      — edit our own fields (description, repo)

Name and color belong to Google and are read-only here; they change in the
Calendar UI and arrive on the next sync.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.clients import labels as labels_store
from app.db.schemas.label import LabelRead, LabelUpdate
from app.services.calendar import labels as calendar_labels

router = APIRouter(prefix="/labels", tags=["labels"])


@router.get("", response_model=list[LabelRead])
async def list_labels(session: Session = Depends(get_session)) -> list[LabelRead]:
    return [LabelRead.from_row(row) for row in labels_store.list_all(session)]


@router.post("/sync", response_model=list[LabelRead])
async def sync_labels(session: Session = Depends(get_session)) -> list[LabelRead]:
    return [LabelRead.from_row(row) for row in await calendar_labels.sync(session)]


@router.patch("/{google_id}", response_model=LabelRead)
async def update_label(
    google_id: str, payload: LabelUpdate, session: Session = Depends(get_session)
) -> LabelRead:
    row = labels_store.get(session, google_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown label")

    if payload.github_repo:
        clash = labels_store.get_by_repo(session, payload.github_repo)
        if clash is not None and clash.google_id != row.google_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{payload.github_repo} is already mapped to the {clash.name} label",
            )

    labels_store.set_metadata(
        session, row, description=payload.description, github_repo=payload.github_repo
    )
    session.commit()
    return LabelRead.from_row(row)
