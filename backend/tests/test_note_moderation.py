from types import SimpleNamespace
import uuid

import pytest

from app.agent.helpers import llm
from app.services.note import moderate
from app.services.note import startup


@pytest.mark.asyncio
async def test_exact_note_is_skipped_and_source_is_recorded(monkeypatch):
    note_id = uuid.uuid4()
    source_id = uuid.uuid4()
    linked = []
    monkeypatch.setattr(
        moderate.notes_store, "find_exact",
        lambda _session, _content: SimpleNamespace(id=note_id, content="Alice prefers afternoons"),
    )
    monkeypatch.setattr(
        moderate.notes_store, "add_source",
        lambda _session, note, source: linked.append((note, source)),
    )
    decision = await moderate.moderate_note(
        object(), " Alice prefers afternoons ", source_raw_input_id=source_id
    )
    assert decision.action == "skip"
    assert linked == [(note_id, source_id)]


@pytest.mark.asyncio
async def test_historical_moderation_accepts_null_initial_checkpoint(monkeypatch):
    class Session:
        committed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, params=None):
            sql = str(statement)
            if "pg_try_advisory_xact_lock" in sql:
                return SimpleNamespace(scalar=lambda: True)
            if "FROM note_moderation_checkpoint" in sql:
                return SimpleNamespace(one=lambda: SimpleNamespace(
                    last_created_at=None, last_note_id=None,
                ))
            assert "CAST(:last_at AS timestamptz) IS NULL" in sql
            assert params == {"last_at": None, "last_id": None}
            return SimpleNamespace(first=lambda: None)

        def commit(self):
            self.committed = True

    session = Session()
    monkeypatch.setattr(startup, "SessionLocal", lambda: session)
    await startup.moderate_existing_notes()
    assert session.committed


@pytest.mark.asyncio
async def test_related_note_can_merge_without_creating_another(monkeypatch):
    note_id = uuid.uuid4()
    candidate = SimpleNamespace(id=note_id, content="Alice prefers afternoons")
    updates = []
    linked = []
    monkeypatch.setattr(moderate.notes_store, "find_exact", lambda *_: None)
    monkeypatch.setattr(moderate, "embed", lambda _text: _embed())
    monkeypatch.setattr(
        moderate.notes_store, "search_similar", lambda *_a, **_k: [candidate]
    )
    monkeypatch.setattr(
        moderate.notes_store, "update", lambda *_a, **kwargs: updates.append(kwargs)
    )
    monkeypatch.setattr(
        moderate.notes_store, "add_source", lambda *_args: linked.append(True)
    )

    async def fake_chat(*_args, **_kwargs):
        return SimpleNamespace(tool_calls=[SimpleNamespace(
            name="moderate_note", input={
                "action": "merge", "target_id": str(note_id),
                "content": "Alice prefers afternoons and uses Zoom for meetings.",
            }
        )])

    monkeypatch.setattr(llm, "chat", fake_chat)
    decision = await moderate.moderate_note(
        object(), "Alice uses Zoom for meetings", source_raw_input_id=uuid.uuid4()
    )
    assert decision.action == "merge"
    assert decision.note_id == note_id
    assert updates[0]["content"] == "Alice prefers afternoons and uses Zoom for meetings."
    assert linked == [True]


@pytest.mark.asyncio
async def test_existing_duplicate_absorbs_sources_and_removes_later_note(monkeypatch):
    older = SimpleNamespace(id=uuid.uuid4(), content="Alice prefers afternoons")
    later = SimpleNamespace(id=uuid.uuid4(), content=older.content)
    actions = []
    monkeypatch.setattr(moderate.notes_store, "find_exact_before",
                        lambda _session, _content, note: older if note is later else None)
    monkeypatch.setattr(moderate.notes_store, "absorb_sources",
                        lambda _session, target, source: actions.append(("absorb", target, source.id)))
    monkeypatch.setattr(moderate.notes_store, "delete",
                        lambda _session, note_id: actions.append(("delete", note_id)))

    decision = await moderate.moderate_note(object(), later.content, existing=later,
                                            raise_on_error=True)

    assert decision.note_id == older.id
    assert actions == [("absorb", older.id, later.id), ("delete", later.id)]


async def _embed():
    return [0.1]
