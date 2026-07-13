from app.db.schemas.search import SearchHit
from app.services.search.suggest import _drop_documents_shadowed_by_tasks


def _hit(**kw) -> SearchHit:
    base = {"id": "x", "title": "t", "type": "task", "score": 1.0}
    base.update(kw)
    return SearchHit(**base)


def test_document_linked_to_shown_task_is_dropped():
    task = _hit(type="task", id="T1", score=2.0)
    kotx = _hit(type="document", id="D1", task_id="T1", source="kotx", score=1.0)
    kept = _drop_documents_shadowed_by_tasks([task, kotx])
    assert [h.id for h in kept] == ["T1"]


def test_document_whose_task_did_not_match_survives():
    other_task = _hit(type="task", id="T9", score=2.0)
    kotx = _hit(type="document", id="D1", task_id="T1", source="kotx", score=1.0)
    kept = _drop_documents_shadowed_by_tasks([other_task, kotx])
    assert {h.id for h in kept} == {"T9", "D1"}


def test_calendar_document_without_task_is_untouched():
    task = _hit(type="task", id="T1", score=2.0)
    cal = _hit(type="document", id="D2", task_id=None, source="calendar", score=1.0)
    kept = _drop_documents_shadowed_by_tasks([task, cal])
    assert {h.id for h in kept} == {"T1", "D2"}
