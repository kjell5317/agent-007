"""Database package.

Re-exports the engine + session helpers and the declarative `Base` so callers
can `from app.db import get_session, Base, SessionLocal` without knowing which
submodule provides each piece.

Subpackages:
  * `models`   — SQLAlchemy ORM models
  * `schemas`  — Pydantic DTOs for API + agent layers
  * `clients`  — DB-facing CRUD helpers (formerly `app.storage`)
"""

from app.db.engine import Base, SessionLocal, engine, get_session

__all__ = ["Base", "SessionLocal", "engine", "get_session"]
