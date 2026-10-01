"""Track search-result opens and list the most frequently opened results."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.search_result_click import SearchResultClick
from app.db.schemas.search import SearchHit


def record(session: Session, hit: SearchHit) -> None:
    snapshot = hit.model_dump(mode="json")
    statement = insert(SearchResultClick).values(
        hit_type=hit.type,
        hit_id=hit.id,
        hit=snapshot,
        click_count=1,
    )
    session.execute(statement.on_conflict_do_update(
        index_elements=[SearchResultClick.hit_type, SearchResultClick.hit_id],
        set_={
            "hit": snapshot,
            "click_count": SearchResultClick.click_count + 1,
            "last_clicked_at": func.now(),
        },
    ))
    session.commit()


def popular(session: Session, *, limit: int = 10) -> list[SearchHit]:
    rows = session.execute(
        select(SearchResultClick.hit)
        .order_by(SearchResultClick.click_count.desc(), SearchResultClick.last_clicked_at.desc())
        .limit(limit)
    ).scalars().all()
    return [SearchHit.model_validate(row) for row in rows]
