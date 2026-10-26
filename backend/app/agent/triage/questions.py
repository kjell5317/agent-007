"""The Jev question catalog for triage, task fields and scheduling attributes.

Choices always carry an explicit way out ("none"): without one Jev drifts to
the first option, and the first option also wins close calls — so it is the
side whose mistake is cheaper (a task over a dropped input, "no match" over a
wrong merge).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.agent.helpers.jev import Choice, Noul, Question, Score

WORK_TYPE_CRITERIA = {
    "physical": "Body, errands, sport, chores, being somewhere in person to do something.",
    "cognitive": "Focused thinking: writing, coding, studying, planning, reading.",
    "social": "Calls, meetings, messaging or talking with people.",
}
EFFORT_CRITERIA = {
    "low": "Light and routine; little concentration or energy needed.",
    "medium": "Needs some concentration or energy.",
    "high": "Demanding: deep focus for a long stretch, or hard physical exertion.",
}
DEADLINE_KIND_CRITERIA = {
    "hard": (
        "Missing the due date makes the task pointless or has external consequences: "
        "submissions, exams, appointments, preparing for a linked event."
    ),
    "firm": "Any other deadline the source states explicitly.",
    "soft": "A guessed or derived due date, or 'sometime' phrasing.",
}
DEADLINE_STAKES_CRITERIA = {
    "firm": "The stated deadline matters, but missing it has no hard consequence.",
    "hard": (
        "Missing the stated deadline makes the task pointless or has external "
        "consequences: a submission, an exam, an appointment, a payment, a booking."
    ),
}

ESTIMATE_MINUTES = (15, 30, 45, 60, 90, 120, 180, 240)
ESTIMATE_LEVELS = (
    "15 minutes or less",
    "About 30 minutes",
    "About 45 minutes",
    "About 1 hour",
    "About 1.5 hours",
    "About 2 hours",
    "About 3 hours",
    "4 hours or more",
)
URGENCY_CRITERIA = {
    "today": "Should be done today.",
    "tomorrow": "Should be done by tomorrow.",
    "this_week": "Should be done within this week.",
    "next_week": "Can wait until next week.",
    "this_month": "Within the next few weeks.",
    "no_rush": "No time pressure at all.",
}
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_DEADLINE = "the deadline by which the user must do what the input asks"
_ABSENT = "The input does not state this, or the deadline is not this kind of date."

HAS_EVENT = Noul(
    instructions=(
        "`input` announces, invites to, or reschedules a specific calendar event "
        "(meeting, appointment, talk, exam, trip) with a date or time."
    ),
)
HAS_FACTS = Noul(
    instructions=(
        "`input` contains a durable fact worth remembering long-term about a person, "
        "account, project or policy (a role, an account or reference number, a standing "
        "arrangement), beyond one-off details of this message."
    ),
)


def triage_questions(candidates: Mapping[str, str]) -> dict[str, Question]:
    """Is it a task, does it match a listed task, should it reach the LLM.
    `candidates` maps candidate id → task title."""
    questions: dict[str, Question] = {
        "is_task": Choice(
            instructions=(
                "Does `input` give the user (the recipient) something concrete to do? "
                "`not_task_precedents` are similar earlier inputs the user did not treat "
                "as tasks; `sender_history` counts how earlier inputs from this sender ended."
            ),
            criteria={
                "task": (
                    "The user must act: reply with an answer, submit, pay, fix, prepare, "
                    "decide, book, register, review or deliver something."
                ),
                "not_task": (
                    "Nothing for the user to do: informational, conversational, marketing, "
                    "a newsletter, an automated notification, or addressed to someone else."
                ),
            },
        ),
        "has_event": HAS_EVENT,
        "has_facts": HAS_FACTS,
    }
    if candidates:
        questions["target"] = Choice(
            instructions=(
                "Is `input` about the same matter as one of the listed `candidates` tasks — "
                "a re-send, a reminder, a copy from another source, or a follow-up on it?"
            ),
            criteria={"none": "A different matter than every listed candidate."}
            | {
                cid: f"The same matter as candidate {cid}: {title}"
                for cid, title in candidates.items()
            },
        )
        questions["target_action"] = Choice(
            instructions=(
                "Assume `input` is about one of the listed `candidates` tasks. "
                "What does it change about that task?"
            ),
            criteria={
                "nothing_new": "Nothing new: a duplicate, a re-send, a restatement or a reminder.",
                "done": "It shows the task is done, resolved, or no longer needed.",
                "changes": "It changes the task's details: deadline, scope, place or facts.",
                "revives": "It brings back a finished task with new work to do.",
            },
        )
    return questions


def field_questions(
    labels: Mapping[str, str],
    *,
    year: int,
    events: Mapping[str, str] | None = None,
    urls: Mapping[str, str] | None = None,
) -> dict[str, Question]:
    """Questions that fill a new task's fields. `labels` maps label name →
    description, `events` event id → summary, `urls` link id → URL."""
    questions: dict[str, Question] = {
        "label": Choice(
            instructions="Which label fits the task in `input` best?",
            criteria=dict(labels) | {"none": "None of the listed labels fits."},
        ),
        "work_type": Choice(
            instructions="What kind of work is the task in `input`?",
            criteria=WORK_TYPE_CRITERIA,
        ),
        "effort": Choice(
            instructions="How demanding is the task in `input`?",
            criteria=EFFORT_CRITERIA,
        ),
        "estimate": Score(
            instructions=(
                "How much focused working time will the user need for the task in `input`?"
            ),
            levels=ESTIMATE_LEVELS,
        ),
        "urgency": Choice(
            instructions=(
                "Ignoring any explicit deadline, how soon should the user get the task in "
                "`input` done, judging by its content?"
            ),
            criteria=URGENCY_CRITERIA,
        ),
        "deadline_stakes": Choice(
            instructions=f"If `input` states {_DEADLINE}, what happens when it is missed?",
            criteria=DEADLINE_STAKES_CRITERIA,
        ),
        "at_home": Noul(
            instructions=(
                "The task in `input` can only be done at home (household chores, laundry, "
                "something that is physically at home)."
            ),
        ),
        "names_place": Noul(
            instructions=(
                "The task in `input` must be done at a specific named place away from home "
                "(an address, a shop, an office, a venue)."
            ),
        ),
        "title_ok": Noul(
            instructions=(
                "`candidate_title` already works as the task's title: it says specifically "
                "what the user must do, and contains no date, time, greeting or filler."
            ),
        ),
        "needs_description": Noul(
            instructions=(
                "To act on the task in `input`, the user needs details the title cannot carry "
                "(steps, reference numbers, amounts, names of people involved)."
            ),
        ),
        "split": Noul(
            instructions=(
                "The task in `input` consists of several independent pieces of work that "
                "could be scheduled separately."
            ),
        ),
        **date_questions(year),
    }
    if events:
        questions["related_event"] = Choice(
            instructions=(
                "Is the task in `input` preparation that must be finished before one of the "
                "listed `upcoming_events` (booking travel for a trip, preparing slides, "
                "buying a gift)?"
            ),
            criteria={"none": "It does not prepare for any listed event."}
            | {eid: f"Prepares for event {eid}: {summary}" for eid, summary in events.items()},
        )
    if urls:
        questions["link"] = Choice(
            instructions=(
                "Which of the listed `links` is the most relevant source page for the task "
                "in `input`?"
            ),
            criteria={"none": "None of them: tracking, unsubscribe or unrelated links."}
            | {uid: url for uid, url in urls.items()},
        )
    return questions


def date_questions(year: int) -> dict[str, Question]:
    """The deadline read as parts; code assembles the date (Jev compares dates
    as text, so it never does the calendar math)."""
    return {
        "due_mode": Choice(
            instructions=(
                f"How is {_DEADLINE} written? 'absolute' = a calendar date naming a month "
                "('August 14', 'the 3rd of March'); 'relative' = relative to today (today, "
                "tonight, tomorrow, the day after tomorrow, a named weekday such as 'by "
                "Friday'); 'none' = the input states no deadline."
            ),
            criteria={"none": None, "absolute": None, "relative": None},
        ),
        "due_month": Choice(
            instructions=f"If {_DEADLINE} is an absolute calendar date, which month is it in?",
            criteria={"none": _ABSENT} | {m: None for m in MONTHS},
        ),
        "due_day": Choice(
            instructions=f"If {_DEADLINE} is an absolute calendar date, which day of the month?",
            criteria={"none": _ABSENT} | {str(d): None for d in range(1, 32)},
        ),
        "due_year": Choice(
            instructions=f"If {_DEADLINE} is an absolute calendar date, which year?",
            criteria={
                "none": "No year is stated.",
                str(year): None,
                str(year + 1): None,
                "other": "A different year is stated.",
            },
        ),
        "due_anchor": Choice(
            instructions=(
                f"If {_DEADLINE} is relative to today, which day is it? 'weekday' = a named "
                "day of the week."
            ),
            criteria={
                "none": _ABSENT,
                "today": "Today or tonight.",
                "tomorrow": None,
                "day_after": "The day after tomorrow.",
                "weekday": None,
            },
        ),
        "due_weekday": Choice(
            instructions=f"If {_DEADLINE} names a day of the week, which one?",
            criteria={"none": _ABSENT} | {w: None for w in WEEKDAYS},
        ),
        "due_week": Choice(
            instructions=(
                f"If {_DEADLINE} names a weekday, which week? 'next' for 'next Thursday' or "
                "'Thursday next week'; 'current' for 'this Thursday'; 'none' for a bare "
                "weekday ('by Thursday')."
            ),
            criteria={"none": _ABSENT, "current": None, "next": None},
        ),
        "due_hour": Choice(
            instructions=f"If {_DEADLINE} includes a time of day, which hour (24-hour clock)?",
            criteria={"none": "No time of day is stated (or only 'end of day')."}
            | {f"{h:02d}": None for h in range(24)},
        ),
        "due_minute": Choice(
            instructions=f"If {_DEADLINE} includes a time of day, which minutes past the hour?",
            criteria={"00": "On the hour, or no time stated.", "15": None, "30": None, "45": None},
        ),
    }


def followup_questions() -> dict[str, Question]:
    """A reply on a thread already linked to `task`."""
    return {
        "target_action": Choice(
            instructions="`input` is a new message about `task`. What does it change about it?",
            criteria={
                "nothing_new": (
                    "Nothing the task needs: an acknowledgement, a thank-you, a restatement "
                    "or a reminder."
                ),
                "done": "It shows the task is done, resolved, or no longer needed.",
                "changes": "It changes the task's details: deadline, scope, place or facts.",
                "revives": "It brings back a finished task with new work to do.",
            },
        ),
        "has_event": HAS_EVENT,
        "has_facts": HAS_FACTS,
    }


def split_question() -> dict[str, Question]:
    return {
        "split": Noul(
            instructions=(
                "The task described in the state consists of several independent pieces of "
                "work that could be scheduled separately."
            ),
        ),
    }


ATTRIBUTE_CRITERIA = {
    "work_type": WORK_TYPE_CRITERIA,
    "effort": EFFORT_CRITERIA,
    "deadline_kind": DEADLINE_KIND_CRITERIA,
}


def attribute_questions(fields: Sequence[str], subject: str) -> dict[str, Question]:
    """Scheduling attributes of one listed task or calendar event."""
    instructions = {
        "work_type": f"What kind of work does this {subject} demand of the user?",
        "effort": f"How demanding is this {subject} for the user?",
        "deadline_kind": f"What kind of deadline does this {subject} have?",
    }
    return {
        name: Choice(instructions=instructions[name], criteria=ATTRIBUTE_CRITERIA[name])
        for name in fields
    }
