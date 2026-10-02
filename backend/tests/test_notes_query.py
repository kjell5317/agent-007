"""Regression coverage for nullable cursor parameters in note search."""

from app.db.clients import notes, search


def test_numeric_note_query_types_null_cursor(monkeypatch):
    class Result:
        def all(self):
            return []

    class Session:
        def execute(self, statement, params):
            sql = str(statement)
            assert sql.count("CAST(:before_at AS timestamptz) IS NULL") == 2
            assert ":before_at IS NULL" not in sql
            assert params["raw_q"] == "3. Januar"
            assert params["before_at"] is None
            return Result()

    assert notes.search_similar(Session(), embedding=[0.1, 0.2], query="3. Januar") == []


def test_note_source_filter_applies_to_both_search_pools():
    class Result:
        def all(self):
            return []

    class Session:
        def execute(self, statement, params):
            sql = str(statement)
            assert sql.count("coalesce(sr.source, 'chat') = :source") == 2
            assert params["source"] == "gmail"
            return Result()

    assert notes.search_similar(
        Session(), embedding=[0.1, 0.2], query="invoice", source="gmail"
    ) == []


def test_numeric_message_search_builds_input_branch():
    sql = search._branch_sql(search.INPUT, match=True, filters_sql="")
    assert "r.source <> 'chat'" in sql
    assert "WHERE r.tsv @@ to_tsquery('english', :tsquery)" in sql


def test_chat_message_display_reads_longer_body_than_suggest():
    class Result:
        def all(self):
            return []

    class Session:
        def execute(self, statement, params):
            assert "left(coalesce(r.content,''), 8001) AS snippet" in str(statement)
            return Result()

    assert search._load_display(Session(), search.INPUT, ["input-id"]) == []
    assert "left(coalesce(r.content,''), 200) AS snippet" in search._BRANCHES[search.INPUT]["select"]
