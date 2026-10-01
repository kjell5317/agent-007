from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.api import search as search_api
from app.db.clients import search_clicks
from app.db.schemas.search import SearchHit


def test_record_search_click_upserts_count_and_latest_hit():
    captured = {}

    class Session:
        def execute(self, statement):
            captured["statement"] = statement

        def commit(self):
            captured["committed"] = True

    hit = SearchHit(type="drive", id="file-1", title="Plan", score=0)
    search_clicks.record(Session(), hit)

    sql = str(captured["statement"].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (hit_type, hit_id) DO UPDATE" in sql
    assert "search_result_clicks.click_count +" in sql
    assert captured["statement"].compile(dialect=postgresql.dialect()).params["hit"]["title"] == "Plan"
    assert captured["committed"] is True


def test_popular_results_are_ordered_by_count_then_recent_open():
    captured = {}

    class Session:
        def execute(self, statement):
            captured["sql"] = str(statement)
            rows = [{"type": "task", "id": "task-1", "title": "Plan", "score": 0}]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    hits = search_clicks.popular(Session(), limit=10)
    assert [hit.id for hit in hits] == ["task-1"]
    assert "click_count DESC, search_result_clicks.last_clicked_at DESC" in captured["sql"]
    assert "LIMIT" in captured["sql"]


def test_search_click_endpoints_use_persistent_store(monkeypatch):
    calls = []
    hit = SearchHit(type="note", id="note-1", title="Idea", score=0)
    monkeypatch.setattr(search_api.search_clicks, "record", lambda session, item: calls.append((session, item)))
    monkeypatch.setattr(search_api.search_clicks, "popular", lambda session, *, limit: [hit])
    session = object()

    search_api.record_result_click(hit, session)
    assert calls == [(session, hit)]
    assert search_api.popular_results(10, session).hits == [hit]
