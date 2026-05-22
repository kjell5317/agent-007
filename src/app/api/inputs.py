"""Generic input intake.

Two entry points:
  * POST /inputs            — manual / programmatic submission of a RawInput
  * POST /inputs/{source}/webhook — dispatch to the registered source's handler

Source-specific routes (e.g. OAuth-driven pollers) live alongside their
implementations once added.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_session
from app.schemas.raw_input import RawInputCreate, RawInputRead

router = APIRouter(prefix="/inputs", tags=["inputs"])


@router.post("", response_model=RawInputRead, status_code=status.HTTP_201_CREATED)
async def submit_input(payload: RawInputCreate, session: Session = Depends(get_session)) -> RawInputRead:
    # TODO: persist RawInput row
    # TODO: enqueue agent processing job
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "submit_input not implemented")


@router.post("/{source}/webhook", status_code=status.HTTP_202_ACCEPTED)
async def source_webhook(source: str, request: Request, session: Session = Depends(get_session)) -> dict:
    # TODO: look up source via app.ingestion.get_source(source)
    # TODO: verify signature (per-provider HMAC) before parsing
    # TODO: call source.handle_webhook(payload, headers), persist + enqueue results
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, f"webhook for {source!r} not implemented")
