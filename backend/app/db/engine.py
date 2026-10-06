from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)
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
