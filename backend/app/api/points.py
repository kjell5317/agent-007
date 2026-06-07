"""Points endpoints — the Points page surface.

  * GET  /points          — current total + configured actions per section
  * POST /points/actions  — submit an action, returns the new total

Action config (names, factors, units) lives in config/points.yaml; the total
and ledger live in the DB. Submission trusts only the server-side config for
the factor — the client just names the action and a quantity.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.clients import points as points_store
from app.points import SECTIONS, load_points_config
from app.services.points import submit_action

router = APIRouter(prefix="/points", tags=["points"])


class ActionRead(BaseModel):
    name: str
    factor: float
    unit: str | None


class SectionRead(BaseModel):
    key: str
    title: str
    actions: list[ActionRead]


class PointsRead(BaseModel):
    total: float
    task_done_factor: float
    sections: list[SectionRead]


class ActionSubmit(BaseModel):
    section: str
    name: str
    quantity: float | None = None


class TotalRead(BaseModel):
    total: float


@router.get("", response_model=PointsRead)
def get_points(session: Session = Depends(get_session)) -> PointsRead:
    cfg = load_points_config()
    sections = [
        SectionRead(
            key=key,
            title=title,
            actions=[
                ActionRead(name=a.name, factor=a.factor, unit=a.unit)
                for a in cfg.sections.get(key, [])
            ],
        )
        for key, title in SECTIONS
    ]
    return PointsRead(
        total=points_store.total(session),
        task_done_factor=cfg.task_done_factor,
        sections=sections,
    )


@router.post("/actions", response_model=TotalRead)
def submit(payload: ActionSubmit, session: Session = Depends(get_session)) -> TotalRead:
    try:
        new_total = submit_action(
            session, section=payload.section, name=payload.name, quantity=payload.quantity
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return TotalRead(total=new_total)
