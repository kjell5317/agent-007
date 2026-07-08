"""CRUD for persisted chat conversations (the chat/"ask" view)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.chat import ChatConversation


def create(session: Session, *, title: str, messages: list) -> ChatConversation:
    row = ChatConversation(title=title, messages=messages)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update(
    session: Session, conversation_id: uuid.UUID, *, title: str, messages: list
) -> ChatConversation | None:
    row = session.get(ChatConversation, conversation_id)
    if row is None:
        return None
    row.title = title
    row.messages = messages
    session.commit()
    session.refresh(row)
    return row


def get(session: Session, conversation_id: uuid.UUID) -> ChatConversation | None:
    return session.get(ChatConversation, conversation_id)


def list_recent(session: Session, *, limit: int = 5) -> list[ChatConversation]:
    stmt = (
        select(ChatConversation)
        .order_by(ChatConversation.updated_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())
