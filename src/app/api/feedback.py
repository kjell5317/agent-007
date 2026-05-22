from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas.feedback import FeedbackCreate, FeedbackRead

router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", response_model=FeedbackRead, status_code=status.HTTP_201_CREATED)
async def submit_feedback(payload: FeedbackCreate, session: Session = Depends(get_session)) -> FeedbackRead:
    # TODO: persist Feedback row
    # TODO: if kind == "duplicate_of", optionally mark the wrong task as duplicate
    # TODO: if kind == "wrong_fields", apply `correction` patch to the task
    # TODO: surface feedback as future few-shot examples (see agent.prompts)
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "submit_feedback not implemented")
