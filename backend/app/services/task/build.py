"""Turn agent-extracted fields into a `TaskCreate` — shared by the LLM
agents, the manual queue and Jev triage."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.agent.helpers.attributes import new_task_attributes
from app.db.clients import labels as labels_store
from app.db.schemas.task import TaskCreate


def task_create_from_fields(
    session: Session, fields: Mapping[str, Any], *, explicit_due: bool
) -> TaskCreate:
    """Event context must already be applied; scheduling attributes default
    from the label and the deadline kind."""
    label = str(fields["label"]) if fields.get("label") else None
    return TaskCreate(
        title=str(fields["title"]),
        description=_text(fields.get("description")),
        estimation=fields.get("estimation") or None,
        estimation_source=fields.get("estimation_source"),
        due_date=fields.get("due_date"),
        location=_text(fields.get("location")),
        link=_text(fields.get("link")),
        label=label,
        related_event_id=fields.get("related_event_id"),
        related_event_calendar_id=fields.get("related_event_calendar_id"),
        related_event_due_derived=bool(fields.get("related_event_due_derived")),
        **new_task_attributes(
            fields,
            labels_store.defaults_by_name(session),
            label=label,
            explicit_due=explicit_due,
            related_event=bool(fields.get("related_event_id")),
        ),
    )


def _text(value: Any) -> str | None:
    return str(value) if value else None
