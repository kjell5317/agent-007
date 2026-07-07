from app.db.clients.search import DOCUMENT, INPUT, TASK
from app.services.search.filters import (
    Filters,
    build_tsquery,
    corpus_restriction,
    parse_query,
)


def test_parse_plain_query_has_no_filters():
    text, filters = parse_query("pay the rent")
    assert text == "pay the rent"
    assert filters == Filters()


def test_parse_peels_filters_and_keeps_free_text():
    text, filters = parse_query("rent source:Gmail label:uni before:2026-06")
    assert text == "rent"
    assert filters.source == "gmail"  # lowercased
    assert filters.label == "uni"
    assert filters.before == "2026-06-01"
    assert filters.after is None


def test_is_status_aliases_normalize():
    assert parse_query("x is:done")[1].status == "closed"
    assert parse_query("x is:dismissed")[1].status == "not_task"
    assert parse_query("x is:open")[1].status == "open"


def test_bad_status_alias_dropped():
    assert parse_query("x is:banana")[1].status is None


def test_date_boundary_precisions():
    assert parse_query("a before:2026")[1].before == "2026-01-01"
    assert parse_query("a before:2026-6")[1].before == "2026-06-01"
    assert parse_query("a after:2026-06-07")[1].after == "2026-06-07"
    assert parse_query("a before:nonsense")[1].before is None


def test_build_tsquery_prefixes_last_token():
    assert build_tsquery("pay ren") == "pay & ren:*"
    assert build_tsquery("rent") == "rent:*"


def test_build_tsquery_empty_and_punctuation():
    assert build_tsquery("") == ""
    assert build_tsquery("   ") == ""
    assert build_tsquery("!!!") == ""


def test_build_tsquery_strips_operators():
    # to_tsquery syntax chars must not leak through as operators.
    assert build_tsquery("a & b | c") == "a & b & c:*"


def test_corpus_restriction_none_for_plain_query():
    assert corpus_restriction(parse_query("rent")[1]) is None


def test_source_spans_input_and_document_corpora():
    # One unified `source:` axis matches an input's source and a document's
    # provider alike, so it opens both corpora.
    assert corpus_restriction(parse_query("x source:gmail")[1]) == frozenset({INPUT, DOCUMENT})
    assert corpus_restriction(parse_query("x source:calendar")[1]) == frozenset({INPUT, DOCUMENT})


def test_task_filters_narrow_to_tasks():
    assert corpus_restriction(parse_query("x is:open")[1]) == frozenset({TASK})
    assert corpus_restriction(parse_query("x label:uni")[1]) == frozenset({TASK})


def test_provider_is_no_longer_a_filter():
    # `provider:` was unified into `source:`; it now stays in the free text.
    text, filters = parse_query("x provider:calendar")
    assert filters == Filters()
    assert "provider:calendar" in text


def test_before_after_do_not_restrict_corpus():
    assert corpus_restriction(parse_query("x before:2026")[1]) is None
