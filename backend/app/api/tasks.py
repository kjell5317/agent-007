"""Task endpoints.

  * GET    /tasks                       — list tasks
  * GET    /tasks/{task_id}             — fetch one
  * POST   /tasks                       — manual create (async via queue)
  * POST   /tasks/open/{raw_input_id}   — promote a raw_input to a task
  * PATCH  /tasks/{task_id}             — edit fields
  * POST   /tasks/{task_id}/close       — mark done
  * POST   /tasks/{task_id}/not_task    — dismiss
  * POST   /tasks/{task_id}/reopen      — revive a closed/dismissed task

All business logic lives in `app.services.task.*` — this file is just
the HTTP surface.
"""

import uuid
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_session
from app.db.clients import route_cache as route_cache_store
from app.db.clients import raw_inputs as raw_inputs_store
from app.db.clients import tasks as tasks_store
from app.db.schemas.task import (
    LocationSuggestionsRead,
    TaskCreationAccepted,
    TaskOpenRequest,
    TaskPromote,
    TaskRawInputRead,
    TaskRead,
    SubtaskSummary,
    TaskUpdate,
)
from app.events import publish_task
from app.services.kotx import (
    KotxConfigError,
    KotxRunError,
    KotxUnsupportedTaskError,
    create_issue_run,
    has_github_url,
)
from app.services.plan import schedule_task, scheduled_interval_for
from app.services.source_url import source_url_for_raw_input
from app.services.task.close import close_task as close_task_svc
from app.services.task.create import create_manual_task
from app.services.task.dismiss import dismiss_task
from app.services.task.open import open_task_from_input
from app.services.task.reopen import enqueue_reopen_task
from app.services.task.split import (
    create_subtask as create_subtask_svc, reflow_children,
    split_deterministically, split_task as split_task_svc,
)
from app.services.task.update import affected_tasks, sync_external, update_task as update_task_svc

router = APIRouter(prefix="/tasks", tags=["tasks"])
log = logging.getLogger(__name__)


async def _sync_task_changes(updates: list[tuple[uuid.UUID, set[str]]], parent_id: uuid.UUID | None = None) -> None:
    with SessionLocal() as session:
        try:
            if parent_id is not None:
                parent = tasks_store.get(session, parent_id)
                if parent is not None and parent.calendar_event_id:
                    from app.services.calendar import delete_task_event
                    from app.config import get_settings
                    await delete_task_event(session, parent)
                    if parent.calendar_event_id and get_settings().google_calendar_id:
                        from app.services.notify import notify_error
                        await notify_error(
                            "Task calendar update failed",
                            RuntimeError("Could not remove the parent calendar event"),
                            context=parent.title,
                        )
            await sync_external(session, updates)
        except Exception as exc:  # noqa: BLE001
            log.exception("task calendar update failed · parent=%s", parent_id)
            from app.services.notify import notify_error
            await notify_error("Task calendar update failed", exc)


async def _reschedule_in_background(task_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        row = tasks_store.get(session, task_id)
        if row is None:
            return
        try:
            result = await schedule_task(session, row, block=scheduled_interval_for(row))
            if result is None:
                raise RuntimeError("Task could not be scheduled")
            publish_task(session, task_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("task rescheduling failed · task=%s", task_id)
            from app.services.notify import notify_error
            await notify_error("Task rescheduling failed", exc, context=row.title)


async def _split_in_background(task_id: uuid.UUID) -> None:
    with SessionLocal() as session:
        row = tasks_store.get(session, task_id)
        if row is None:
            return
        try:
            children = await split_task_svc(session, row, force=True)
            if not children:
                raise ValueError("This task could not be split")
        except Exception as exc:  # noqa: BLE001
            log.exception("task split failed · task=%s", task_id)
            from app.services.notify import notify_error
            await notify_error("Task split failed", exc, context=row.title)


def _to_read(task, status_: str, is_manual: bool, session: Session) -> TaskRead:
    raw = raw_inputs_store.latest_for_task(session, task.id)
    linked_inputs = raw_inputs_store.list_for_task(session, task.id)
    if parent_id := getattr(task, "parent_task_id", None):
        # The parent's original input is the source of each derived subtask.
        linked_inputs.extend(raw_inputs_store.list_for_task(session, parent_id))
    children = tasks_store.children(session, task.id) if getattr(task, "is_container", False) else []
    child_statuses = tasks_store.latest_status_for(session, [child.id for child in children])
    return TaskRead.build(
        task,
        status_,
        is_manual,
        source_url=source_url_for_raw_input(raw),
        raw_inputs=[
            TaskRawInputRead.build(
                linked,
                source_url=source_url_for_raw_input(linked),
            )
            for linked in linked_inputs
        ],
        subtasks=[SubtaskSummary(
            id=child.id, title=child.title, due_date=child.due_date,
            estimation=child.estimation,
            status=child_statuses.get(child.id, "open"),
            depends_on_task_id=child.depends_on_task_id,
        ) for child in children],
    )


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, le=500),
    session: Session = Depends(get_session),
) -> list[TaskRead]:
    rows = tasks_store.list_(session, status=status_filter, limit=limit, include_containers=False)
    manual_map = tasks_store.is_manual_for(session, [t.id for t, _ in rows])
    return [_to_read(t, s, manual_map.get(t.id, False), session) for t, s in rows]


@router.get("/location_suggestions", response_model=LocationSuggestionsRead)
async def location_suggestions(
    q: str = Query("", max_length=512),
    session: Session = Depends(get_session),
) -> LocationSuggestionsRead:
    suggestions = route_cache_store.location_suggestions(session, query=q, limit=3)
    return LocationSuggestionsRead(suggestions=suggestions)


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/split", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def split_task(
    task_id: uuid.UUID, background_tasks: BackgroundTasks, session: Session = Depends(get_session)
) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if row.parent_task_id or row.is_container:
        raise HTTPException(status.HTTP_409_CONFLICT, "This task cannot be split")
    background_tasks.add_task(_split_in_background, task_id)
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/split_deterministic", response_model=TaskRead)
async def split_task_deterministic(
    task_id: uuid.UUID, background_tasks: BackgroundTasks, session: Session = Depends(get_session)
) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    try:
        children = split_deterministically(session, row)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    background_tasks.add_task(
        _sync_task_changes, [(child.id, {"due_date", "estimation"}) for child in children], task_id
    )
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/subtasks", response_model=TaskRead)
async def add_subtask(
    task_id: uuid.UUID, payload: dict[str, str], background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> TaskRead:
    parent = tasks_store.get(session, task_id)
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if parent.parent_task_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A subtask cannot have subtasks")
    try:
        children = tasks_store.children(session, task_id)
        new_child = create_subtask_svc(session, parent, payload.get("title", ""))
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    background_tasks.add_task(
        _sync_task_changes,
        [(child.id, {"due_date"}) for child in children] +
        [(new_child.id, {"due_date", "estimation"})],
        task_id if not children else None,
    )
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(parent, status_, is_manual, session)


@router.put("/{task_id}/subtasks/order", response_model=TaskRead)
async def reorder_subtasks(
    task_id: uuid.UUID, order: list[uuid.UUID], background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> TaskRead:
    parent = tasks_store.get(session, task_id)
    if parent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    children = tasks_store.children(session, task_id)
    by_id = {child.id: child for child in children}
    if len(order) != len(children) or set(order) != set(by_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Order must contain every subtask once")
    reflow_children(session, parent, [by_id[child_id] for child_id in order])
    background_tasks.add_task(
        _sync_task_changes, [(child_id, {"due_date"}) for child_id in order]
    )
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(parent, status_, is_manual, session)


@router.post(
    "", response_model=TaskCreationAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def create_task(
    payload: TaskPromote, session: Session = Depends(get_session)
) -> TaskCreationAccepted:
    user_fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        raw = await create_manual_task(session, user_fields)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return TaskCreationAccepted(raw_input_id=raw.id, status="processing")


@router.post(
    "/open/{raw_input_id}",
    response_model=TaskCreationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_task_from_input(
    raw_input_id: uuid.UUID,
    payload: TaskOpenRequest | None = None,
    session: Session = Depends(get_session),
) -> TaskCreationAccepted:
    """Enqueue a manual override that turns an already-processed raw_input
    into a task. Returns immediately; the client polls
    `GET /inputs/{raw_input_id}` until the row gains a `task_id`."""
    context_input_ids = payload.context_input_ids if payload is not None else []
    target_task_id = payload.target_task_id if payload is not None else None
    user_fields = (
        {
            k: v
            for k, v in payload.model_dump(exclude={"context_input_ids"}).items()
            if v is not None and k not in {"content", "target_task_id"}
        }
        if payload is not None
        else {}
    )
    try:
        await open_task_from_input(
            session, raw_input_id, user_fields, context_input_ids, target_task_id
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return TaskCreationAccepted(raw_input_id=raw_input_id, status="processing")


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, background_tasks: BackgroundTasks,
    session: Session = Depends(get_session)
) -> TaskRead:
    fields = payload.model_dump(exclude_unset=True)
    try:
        row = await update_task_svc(
            session, task_id, fields, sync_calendar=False, auto_split=False
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    background_tasks.add_task(_sync_task_changes, affected_tasks(session, row, set(fields)))
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/reschedule", response_model=TaskRead, status_code=status.HTTP_202_ACCEPTED)
async def reschedule_task(
    task_id: uuid.UUID, background_tasks: BackgroundTasks, session: Session = Depends(get_session)
) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")

    background_tasks.add_task(_reschedule_in_background, task_id)
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/github_issue", response_model=TaskRead)
async def create_github_issue(task_id: uuid.UUID, session: Session = Depends(get_session)) -> TaskRead:
    row = tasks_store.get(session, task_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
    if has_github_url(row.link):
        raise HTTPException(status.HTTP_409_CONFLICT, "Task already has a GitHub URL")

    try:
        run = await create_issue_run(row)
    except KotxUnsupportedTaskError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KotxConfigError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except KotxRunError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    row.link = run.issue_url
    session.commit()
    publish_task(session, task_id)
    status_ = tasks_store.latest_status_for(session, [task_id]).get(task_id, "open")
    is_manual = tasks_store.is_manual_for(session, [task_id]).get(task_id, False)
    return _to_read(row, status_, is_manual, session)


@router.post("/{task_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    try:
        await close_task_svc(session, task_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{task_id}/not_task", status_code=status.HTTP_204_NO_CONTENT)
async def mark_not_task(task_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    try:
        await dismiss_task(session, task_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post(
    "/{task_id}/reopen",
    response_model=TaskCreationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reopen_task(
    task_id: uuid.UUID, session: Session = Depends(get_session)
) -> TaskCreationAccepted:
    try:
        raw = await enqueue_reopen_task(session, task_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return TaskCreationAccepted(raw_input_id=raw.id, status="processing")
