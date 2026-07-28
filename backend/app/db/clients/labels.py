"""CRUD for the mirrored Google Calendar labels.

Google-owned fields (`name`, `background_color`) are only ever written by
`upsert_from_google`; the description and repo mapping are written by the user
through the labels settings page.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models.label import Label


def list_all(session: Session) -> list[Label]:
    return list(session.scalars(select(Label).order_by(Label.name)))


def get(session: Session, google_id: str) -> Label | None:
    return session.get(Label, google_id)


def upsert_from_google(
    session: Session, *, google_id: str, name: str, background_color: str
) -> Label:
    row = session.get(Label, google_id)
    if row is None:
        row = Label(google_id=google_id, name=name, background_color=background_color)
        session.add(row)
    else:
        row.name = name
        row.background_color = background_color
    session.flush()
    return row


def prune_missing(session: Session, google_ids: list[str]) -> int:
    """Drop rows for labels that no longer exist on the calendar.

    A `calendars.get` response carries the complete label list, so anything
    absent from it was deleted in Google and its local metadata is dead.
    """
    stmt = delete(Label)
    if google_ids:
        stmt = stmt.where(Label.google_id.not_in(google_ids))
    return session.execute(stmt).rowcount


def set_metadata(
    session: Session, row: Label, *, description: str, github_repo: str | None
) -> Label:
    row.description = description
    row.github_repo = github_repo
    session.flush()
    return row


def agent_descriptions(session: Session) -> dict[str, str]:
    """`{name: description}` for the labels the agent may choose from.

    A label with no description carries nothing for the model to match on, so
    it stays out of the tool enum until the user writes one.
    """
    rows = session.scalars(
        select(Label).where(Label.description != "").order_by(Label.name)
    )
    return {row.name: row.description for row in rows}


def google_id_for(session: Session, name: str | None) -> str | None:
    """The Calendar `eventLabelId` for a task's label name, if it still exists."""
    if not name:
        return None
    return session.scalar(select(Label.google_id).where(Label.name == name))


def get_by_repo(session: Session, repo: str) -> Label | None:
    """The label mapped to an `owner/repo`, matched case-insensitively."""
    return session.scalar(
        select(Label).where(func.lower(Label.github_repo) == repo.strip().lower())
    )
