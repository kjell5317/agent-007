from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
# One process holds connections across awaits from every background loop plus
# the solver's parallel route lookups. The default 5+10 ran dry under that, and
# a checkout made on the event loop then freezes it — the holders can't resume
# to release — until the 30 s pool timeout. Postgres allows 100.
engine = create_engine(
    _settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=30, future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a DB session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def release_read_connection(session: Session) -> None:
    """Return a completed read's connection before slow async work begins.

    Call only after the result has been materialized. Pending ORM writes keep
    their transaction, so this cannot silently discard a caller's changes.
    """
    if isinstance(session, Session) and not (session.new or session.dirty or session.deleted):
        session.rollback()
