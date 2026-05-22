from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas.feedback import FeedbackCreate, FeedbackRead
from app.storage import feedback as feedback_store

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackRead, status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    payload: FeedbackCreate, session: Session = Depends(get_session)
) -> FeedbackRead:
    row = feedback_store.create(session, payload)
    session.commit()
    # TODO: if kind == "wrong_fields", apply `correction` patch to the task
    # TODO: surface feedback as future few-shot examples (see agent.prompts)
    return FeedbackRead.model_validate(row)
