"""Versioned fitted parameters, one active row per kind."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models.scheduling import LearnedModel

# Old versions are history only; keep a few for comparing metrics.
KEEP_VERSIONS = 20


def active(session: Session, kind: str) -> LearnedModel | None:
    return session.scalars(
        select(LearnedModel).where(LearnedModel.kind == kind, LearnedModel.active.is_(True))
    ).first()


def publish(
    session: Session,
    *,
    kind: str,
    params: dict,
    n_samples: int,
    metrics: dict | None = None,
) -> LearnedModel:
    """Insert a new version, make it the only active one, prune old versions."""
    version = (session.scalar(
        select(func.max(LearnedModel.version)).where(LearnedModel.kind == kind)
    ) or 0) + 1
    session.execute(
        update(LearnedModel)
        .where(LearnedModel.kind == kind, LearnedModel.active.is_(True))
        .values(active=False)
    )
    row = LearnedModel(
        kind=kind, version=version, params=params, n_samples=n_samples,
        metrics=metrics, active=True,
    )
    session.add(row)
    session.flush()
    stale = list(session.scalars(
        select(LearnedModel)
        .where(LearnedModel.kind == kind, LearnedModel.version <= version - KEEP_VERSIONS)
    ))
    for old in stale:
        session.delete(old)
    session.flush()
    return row
