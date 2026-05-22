from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.feedback import Feedback
from app.schemas.feedback import FeedbackCreate


def create(session: Session, payload: FeedbackCreate) -> Feedback:
    row = Feedback(
        task_id=payload.task_id,
        raw_input_id=payload.raw_input_id,
        kind=payload.kind,
        note=payload.note,
        correction=payload.correction,
    )
    session.add(row)
    session.flush()
    return row
