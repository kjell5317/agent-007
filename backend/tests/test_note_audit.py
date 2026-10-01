"""Note activity projection and manual edit attribution."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.api import notes as notes_api
from app.db.clients import notes as notes_store


def test_note_list_exposes_content_update_count_and_latest_time():
    now = datetime.now(timezone.utc)
    note_id = uuid.uuid4()

    class Result:
        def all(self):
            return [SimpleNamespace(
                id=note_id, content="Fact", source_raw_input_id=None,
                source_raw_input_ids=[], needs_review=False,
                created_at=now, updated_at=now, source=None,
                source_from=None, source_subject=None,
                history_count=3, content_update_count=2, last_content_update_at=now,
            )]

    class Session:
        def execute(self, statement, params):
            sql = str(statement)
            assert "count(*) AS history_count" in sql
            assert "count(*) FILTER (WHERE action = 'content_updated')" in sql
            assert "ORDER BY updated_at DESC" in sql
            return Result()

    item = notes_store.list_all(Session())[0]
    assert item.history_count == 3
    assert item.content_update_count == 2
    assert item.last_content_update_at == now


@pytest.mark.asyncio
async def test_manual_edit_marks_audit_actor_for_transaction(monkeypatch):
    note_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    statements = []

    class Session:
        def execute(self, statement):
            statements.append(str(statement))

        def commit(self):
            pass

    async def fake_embed(content):
        return [0.1]

    monkeypatch.setattr(notes_api, "embed", fake_embed)
    monkeypatch.setattr(notes_api.notes_store, "update", lambda *a, **kw: True)
    monkeypatch.setattr(notes_api.notes_store, "get_item", lambda *a: notes_store.NoteListItem(
        id=note_id, content="Edited fact", source_raw_input_id=None,
        created_at=now, updated_at=now, source=None,
        source_from=None, source_subject=None,
        content_update_count=1, last_content_update_at=now,
    ))
    result = await notes_api.update_note(
        note_id, notes_api.NoteUpdate(content="Edited fact"), Session(),
    )
    assert result.content_update_count == 1
    assert "set_config('app.note_actor', 'manual', true)" in statements[0]
