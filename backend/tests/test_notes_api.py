from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from fastapi import HTTPException  # noqa: E402

from app.api import notes as notes_api  # noqa: E402
from app.db.clients.notes import NoteListItem  # noqa: E402


def _item(note_id: uuid.UUID, content: str = "Alice prefers morning meetings") -> NoteListItem:
    return NoteListItem(
        id=note_id,
        content=content,
        source_raw_input_id=uuid.uuid4(),
        created_at=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc),
        source="gmail",
        source_from="alice@example.com",
        source_subject="Scheduling",
    )


@pytest.mark.asyncio
async def test_list_notes_returns_enriched_reads(monkeypatch):
    item = _item(uuid.uuid4())
    monkeypatch.setattr(notes_api.notes_store, "list_all", lambda *_a, **_k: [item])

    reads = await notes_api.list_notes(session=object())

    assert len(reads) == 1
    payload = reads[0].model_dump(mode="json")
    assert payload["content"] == item.content
    assert payload["source"] == "gmail"
    assert payload["source_from"] == "alice@example.com"


@pytest.mark.asyncio
async def test_update_note_reembeds_and_returns_updated(monkeypatch):
    note_id = uuid.uuid4()
    embedded: list[str] = []

    async def fake_embed(text, **_kw):
        embedded.append(text)
        return [0.1] * 4

    updated: dict = {}

    def fake_update(_session, nid, *, content, embedding):
        updated["id"] = nid
        updated["content"] = content
        updated["embedding"] = embedding
        return True

    monkeypatch.setattr(notes_api, "embed", fake_embed)
    monkeypatch.setattr(notes_api.notes_store, "update", fake_update)
    monkeypatch.setattr(
        notes_api.notes_store,
        "get_item",
        lambda _s, nid: _item(nid, content="Alice prefers afternoons"),
    )
    session = _CommitSpy()

    read = await notes_api.update_note(
        note_id, notes_api.NoteUpdate(content="  Alice prefers afternoons  "), session=session
    )

    assert embedded == ["Alice prefers afternoons"]  # trimmed before embedding
    assert updated["content"] == "Alice prefers afternoons"
    assert updated["embedding"] == [0.1] * 4
    assert read.content == "Alice prefers afternoons"
    assert session.committed


@pytest.mark.asyncio
async def test_update_note_rejects_empty_content(monkeypatch):
    async def fake_embed(*_a, **_k):
        raise AssertionError("should not embed empty content")

    monkeypatch.setattr(notes_api, "embed", fake_embed)

    with pytest.raises(HTTPException) as exc:
        await notes_api.update_note(
            uuid.uuid4(), notes_api.NoteUpdate(content="   "), session=object()
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_update_note_missing_is_404(monkeypatch):
    async def fake_embed(*_a, **_k):
        return None

    monkeypatch.setattr(notes_api, "embed", fake_embed)
    monkeypatch.setattr(notes_api.notes_store, "update", lambda *_a, **_k: False)

    with pytest.raises(HTTPException) as exc:
        await notes_api.update_note(
            uuid.uuid4(), notes_api.NoteUpdate(content="something"), session=_CommitSpy()
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_note_commits(monkeypatch):
    monkeypatch.setattr(notes_api.notes_store, "delete", lambda *_a, **_k: True)
    session = _CommitSpy()

    result = await notes_api.delete_note(uuid.uuid4(), session=session)

    assert result is None
    assert session.committed


@pytest.mark.asyncio
async def test_delete_note_missing_is_404(monkeypatch):
    monkeypatch.setattr(notes_api.notes_store, "delete", lambda *_a, **_k: False)

    with pytest.raises(HTTPException) as exc:
        await notes_api.delete_note(uuid.uuid4(), session=_CommitSpy())
    assert exc.value.status_code == 404


class _CommitSpy:
    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True
