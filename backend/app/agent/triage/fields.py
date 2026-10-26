"""A new task's fields from Jev answers, plus what is left for the writer LLM."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from app.agent.helpers.jev import Answer
from app.agent.triage.dates import assemble_due, due_from_urgency
from app.agent.triage.policy import yes
from app.agent.triage.questions import ESTIMATE_MINUTES
from app.agent.triage.state import StateRefs
from app.services.learn.jev import Thresholds


@dataclass(frozen=True)
class FieldDraft:
    label: str | None
    work_type: str | None
    effort: str | None
    deadline_kind: str | None
    estimation: int
    due_date: datetime | None
    due_date_is_explicit: bool
    related_event: tuple[str, str] | None
    location: str | None
    link: str | None
    split: bool
    # Left for the writer LLM:
    needs_title: bool
    needs_description: bool
    needs_due_date: bool
    needs_location: bool

    def writer_needs(self) -> set[str]:
        flags = {
            "title": self.needs_title,
            "description": self.needs_description,
            "due_date": self.needs_due_date,
            "location": self.needs_location,
        }
        return {name for name, needed in flags.items() if needed}

    def task_fields(self) -> dict:
        out: dict = {
            "label": self.label,
            "work_type": self.work_type,
            "effort": self.effort,
            "estimation": self.estimation,
            "due_date": self.due_date,
            "location": self.location,
            "link": self.link,
        }
        if self.deadline_kind:
            out["deadline_kind"] = self.deadline_kind
        if self.related_event:
            out["related_event_id"], out["related_event_calendar_id"] = self.related_event
        return {k: v for k, v in out.items() if v is not None}


def draft_fields(
    answers: Mapping[str, Answer], refs: StateRefs, *, now: datetime, th: Thresholds
) -> FieldDraft:
    def confident(name: str) -> str | None:
        answer = answers.get(name)
        if answer is None or answer.value == "none" or answer.p < th.field:
            return None
        return answer.value

    due_date, explicit, needs_due = _due(answers, now, th)
    event = confident("related_event")
    link = confident("link")
    return FieldDraft(
        label=confident("label"),
        work_type=confident("work_type"),
        effort=confident("effort"),
        # Without a stated deadline, `new_task_attributes` derives soft / hard
        # (linked event) itself.
        deadline_kind=confident("deadline_stakes") if explicit else None,
        estimation=ESTIMATE_MINUTES[int(answers["estimate"].value)],
        due_date=due_date,
        due_date_is_explicit=explicit,
        related_event=refs.events.get(event) if event else None,
        location="home" if yes(answers.get("at_home")) >= 0.7 else None,
        link=refs.urls.get(link) if link else None,
        split=yes(answers.get("split")) >= 0.5,
        needs_title=not refs.title or yes(answers.get("title_ok")) < th.title_ok,
        needs_description=yes(answers.get("needs_description")) >= 0.5,
        needs_due_date=needs_due,
        needs_location=yes(answers.get("names_place")) >= 0.5 and yes(answers.get("at_home")) < 0.7,
    )


def _due(
    answers: Mapping[str, Answer], now: datetime, th: Thresholds
) -> tuple[datetime | None, bool, bool]:
    """`(due, explicit, needs the writer)`. A stated deadline Jev can't read
    confidently goes to the writer; without one the urgency bucket sets it."""
    mode = answers["due_mode"]
    if mode.value == "none" and mode.p >= th.date:
        return due_from_urgency(answers["urgency"].value, now), False, False
    due, weakest = assemble_due(answers, now)
    if due is not None and weakest >= th.date:
        return due, True, False
    return None, False, True
