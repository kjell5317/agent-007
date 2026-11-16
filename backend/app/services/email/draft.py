"""Reply drafts for email tasks: gather the thread, write the reply, put it
in the mailbox, and follow it until the user sends or discards it.

007 never sends: the Gmail client has no send method. Sent detection comes
from the poll, which sees the user's SENT message on the draft's thread.
"""

from __future__ import annotations

import logging
from contextlib import aclosing
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.agent.draft.runner import ThreadMessage, write_reply
from app.auth.google_tokens import get_fresh_google_token
from app.config import get_settings
from app.db.clients import email_drafts as drafts_store
from app.db.clients import handoffs as handoffs_store
from app.db.clients import raw_inputs as raw_inputs_store
from app.db.models.email_draft import EmailDraft
from app.services.email.compose import (
    Sender,
    build_reply,
    primary_sender,
    recipient_address,
    reply_recipient,
    reply_subject,
)
from app.services.input.gmail.client import GmailClient
from app.services.input.gmail.preprocess import preprocess_message
from app.services.notify import (
    clear_notification_tag,
    handoff_tag,
    notify_draft_ready,
    notify_draft_stale,
)

log = logging.getLogger(__name__)

COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
_THREAD_MESSAGES = 8
_MESSAGE_CHARS = 3000
_EXAMPLES = 3
_EXAMPLE_CHARS = 1500


@dataclass
class _Reply:
    sender: Sender
    to: str
    subject: str
    body: str
    open_questions: list[str]
    message: bytes
    trace: dict


def mail_url(account: str, thread_id: str) -> str:
    return f"https://mail.google.com/mail/?authuser={quote(account)}#all/{thread_id}"


def draft_mail_url(session: Session, draft: EmailDraft) -> str:
    raw = raw_inputs_store.get(session, draft.raw_input_id) if draft.raw_input_id else None
    account = str(((raw.source_metadata or {}) if raw is not None else {}).get("account") or "")
    return mail_url(account, draft.provider_thread_ref or "")


def _gmail_input(session: Session, task_id):
    rows = [r for r in raw_inputs_store.list_for_task(session, task_id) if r.source == "gmail"]
    if not rows:
        raise ValueError("This task has no email to reply to")
    return rows[0]


def _thread_id(meta: dict) -> str:
    # GitHub mails carry a cross-source `thread_id`; the Gmail one is kept aside.
    return str(meta.get("gmail_thread_id") or meta.get("thread_id") or "")


async def _client(session: Session, account: str | None) -> GmailClient:
    token = await get_fresh_google_token(session, account_key=account)
    if token.scopes and COMPOSE_SCOPE not in token.scopes:
        raise RuntimeError("Google access lacks gmail.compose: reconnect Google to allow drafts")
    return GmailClient(token.access_token)


async def _style_examples(client: GmailClient, me: list[str], recipient: str) -> list[str]:
    domain = recipient.rsplit("@", 1)[-1] if "@" in recipient else ""
    queries = [q for q in (f"in:sent to:{recipient}" if recipient else "",
                           f"in:sent to:{domain}" if domain else "", "in:sent") if q]
    for query in queries:
        ids: list[str] = []
        # Stop after the first page: the generator would page through all sent mail.
        async with aclosing(client.list_messages(query=query, max_results=_EXAMPLES)) as stream:
            async for message_id in stream:
                ids.append(message_id)
                if len(ids) == _EXAMPLES:
                    break
        examples = []
        for message_id in ids:
            raw = await client.get_message(message_id)
            if raw is not None:
                body = preprocess_message(raw, my_addresses=me).body
                if body.strip():
                    examples.append(body[:_EXAMPLE_CHARS])
        if examples:
            return examples
    return []


async def _compose(session: Session, client: GmailClient, task, raw, brief: str) -> _Reply:
    meta = raw.source_metadata or {}
    account = str(meta.get("account") or "")
    me = [account, *get_settings().my_email_addresses]
    thread = await client.get_thread(_thread_id(meta))
    # Our own earlier draft sits in the thread too.
    messages = [m for m in thread.get("messages") or [] if "DRAFT" not in (m.get("labelIds") or [])]
    cleaned = [(m, preprocess_message(m, my_addresses=me)) for m in messages[-_THREAD_MESSAGES:]]
    incoming = [r for m, r in cleaned if "SENT" not in (m.get("labelIds") or [])]
    original, original_body = (
        (incoming[-1].metadata, incoming[-1].body) if incoming else (meta, raw.content or "")
    )
    to = reply_recipient(original)
    if not to:
        raise ValueError("The email has no sender to reply to")
    sender = primary_sender(await client.list_send_as(), account)
    examples = await _style_examples(client, me, recipient_address(to))
    written = await write_reply(
        session,
        task=task,
        brief=brief,
        recipient=to,
        thread=[
            ThreadMessage(
                sender=str(r.metadata.get("from") or ""),
                date=str(r.metadata.get("date") or ""),
                body=r.body[:_MESSAGE_CHARS],
            )
            for _, r in cleaned
        ],
        examples=examples,
    )
    message = build_reply(
        from_header=sender.from_header, meta=original, body=written.body,
        signature=sender.signature, original_body=original_body,
    )
    return _Reply(
        sender=sender, to=to, subject=reply_subject(original.get("subject")), body=written.body,
        open_questions=written.open_questions, message=message, trace=written.trace,
    )


def _note(reply: _Reply) -> str | None:
    return "Fill in: " + "; ".join(reply.open_questions) if reply.open_questions else None


async def create_reply_draft(session: Session, task, *, brief: str) -> tuple[EmailDraft, dict]:
    """The draft plus the writing agent's trace."""
    if drafts_store.live_for_task(session, task.id) is not None:
        # A second draft hand-off rewrites the draft instead of adding one.
        return await regenerate(session, task)
    raw = _gmail_input(session, task.id)
    meta = raw.source_metadata or {}
    account, thread_id = str(meta.get("account") or ""), _thread_id(meta)
    client = await _client(session, account)
    reply = await _compose(session, client, task, raw, brief)
    created = await client.create_draft(reply.message, thread_id)
    draft = drafts_store.create(
        session,
        task_id=task.id,
        raw_input_id=raw.id,
        from_address=reply.sender.address,
        to=reply.to,
        subject=reply.subject,
        body=reply.body,
        open_questions=reply.open_questions,
        provider_draft_id=created.get("id"),
        provider_thread_ref=thread_id,
        state="pushed",
    )
    await notify_draft_ready(task, mail_url=mail_url(account, thread_id), note=_note(reply))
    return draft, reply.trace


async def regenerate(session: Session, task) -> tuple[EmailDraft, dict]:
    draft = drafts_store.live_for_task(session, task.id)
    if draft is None:
        raise LookupError("No draft on this task")
    raw = raw_inputs_store.get(session, draft.raw_input_id) if draft.raw_input_id else None
    raw = raw or _gmail_input(session, task.id)
    meta = raw.source_metadata or {}
    account, thread_id = str(meta.get("account") or ""), _thread_id(meta)
    handoff = handoffs_store.latest_for_task(session, task.id)
    brief = handoff.brief if handoff is not None and handoff.kind == "email_reply" else task.title
    client = await _client(session, account)
    reply = await _compose(session, client, task, raw, brief)
    updated = (
        await client.update_draft(draft.provider_draft_id, reply.message, thread_id)
        if draft.provider_draft_id else None
    )
    if updated is None:
        updated = await client.create_draft(reply.message, thread_id)
    draft.provider_draft_id = updated.get("id") or draft.provider_draft_id
    draft.from_address, draft.to, draft.subject = reply.sender.address, reply.to, reply.subject
    draft.body, draft.open_questions, draft.state = reply.body, reply.open_questions, "pushed"
    if handoff is not None and handoff.kind == "email_reply":
        handoff.trace = reply.trace
    session.commit()
    await notify_draft_ready(task, mail_url=mail_url(account, thread_id), note=_note(reply))
    return draft, reply.trace


async def regenerate_in_background(task_id) -> None:
    from app.db import SessionLocal
    from app.db.clients import tasks as tasks_store
    from app.events import publish_task
    from app.services.notify import notify_error

    with SessionLocal() as session:
        task = tasks_store.get(session, task_id)
        if task is None:
            return
        try:
            await regenerate(session, task)
        except Exception as exc:  # noqa: BLE001 — background work reports through a push
            log.exception("draft regeneration failed · task=%s", task_id)
            await notify_error("Draft regeneration failed", exc, context=task.title)
            return
        publish_task(session, task_id)


async def discard(session: Session, task) -> None:
    draft = drafts_store.live_for_task(session, task.id)
    if draft is None:
        raise LookupError("No draft on this task")
    if draft.provider_draft_id:
        raw = raw_inputs_store.get(session, draft.raw_input_id) if draft.raw_input_id else None
        meta = (raw.source_metadata or {}) if raw is not None else {}
        client = await _client(session, meta.get("account") or None)
        await client.delete_draft(draft.provider_draft_id)
    draft.state = "discarded"
    handoff = handoffs_store.latest_for_task(session, task.id)
    if handoff is not None and handoff.kind == "email_reply":
        handoff.state = "declined"
    session.commit()
    await clear_notification_tag(handoff_tag(task.id))


async def mark_stale(session: Session, task, raw) -> None:
    """A new message arrived on the thread after the draft was written."""
    draft = drafts_store.live_for_task(session, task.id)
    if draft is None or draft.state != "pushed":
        return
    draft.state = "stale"
    session.commit()
    meta = raw.source_metadata or {}
    await notify_draft_stale(task, mail_url=mail_url(str(meta.get("account") or ""), _thread_id(meta)))


async def mark_sent(session: Session, client: GmailClient, thread_ids: set[str]) -> int:
    """The user sent mail on a thread 007 drafted a reply for: the reply task
    is done. A draft the user didn't use is removed from the mailbox."""
    from app.services.task.close import close_task

    drafts = drafts_store.live_for_threads(session, thread_ids)
    for draft in drafts:
        if draft.provider_draft_id:
            try:
                await client.delete_draft(draft.provider_draft_id)
            except Exception:  # noqa: BLE001 — a leftover draft must not block closing the task
                log.exception("leftover draft delete failed · draft=%s", draft.id)
        draft.state = "sent"
        session.commit()
        try:
            await close_task(session, draft.task_id)
        except (LookupError, ValueError):
            pass
        await clear_notification_tag(handoff_tag(draft.task_id))
    return len(drafts)
