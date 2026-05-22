"""Generic input intake.

Two entry points:
  * POST /inputs            — manual / programmatic submission of a RawInput
  * POST /inputs/{source}/webhook — dispatch to the registered source's handler

Source-specific routes (e.g. OAuth-driven pollers) live alongside their
implementations once added.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.agent.runner import process_raw_input
from app.db import SessionLocal, get_session
from app.schemas.raw_input import RawInputCreate, RawInputRead
from app.storage import raw_inputs

router = APIRouter(prefix="/inputs", tags=["inputs"])


async def _process_in_background(raw_input_id) -> None:
    """Open a fresh session for the agent run; BackgroundTasks runs after the
    response, so the request-scoped session is already closed."""
    session = SessionLocal()
    try:
        await process_raw_input(session, raw_input_id)
    finally:
        session.close()


@router.post("", response_model=RawInputRead, status_code=status.HTTP_201_CREATED)
async def submit_input(
    payload: RawInputCreate,
    background: BackgroundTasks,
    run_sync: bool = False,
    session: Session = Depends(get_session),
) -> RawInputRead:
    row = raw_inputs.create(session, payload)
    session.commit()

    if run_sync:
        await process_raw_input(session, row.id)
    else:
        background.add_task(_process_in_background, row.id)

    return RawInputRead.model_validate(row)


@router.post("/{source}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def source_webhook(source: str, request: Request, session: Session = Depends(get_session)) -> dict:
    # TODO: look up source via app.ingestion.get_source(source), verify signature,
    # call source.handle_webhook, persist + enqueue results.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"webhook for {source!r} not implemented")
