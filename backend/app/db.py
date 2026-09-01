"""SQLAlchemy engine/session setup."""

import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(settings.sqlalchemy_database_url)

# Schema `server_default`s use Postgres's `now()` — this app targets Postgres
# in every real environment. SQLite (used only for quick local scripts/tests,
# see AGENTS.md) doesn't know that function, so register it as a no-op shim
# purely so those defaults don't blow up outside Postgres. Never used against
# the real database.
if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _register_sqlite_now(dbapi_connection: sqlite3.Connection, _) -> None:
        dbapi_connection.create_function(
            "now", 0, lambda: datetime.now(UTC).isoformat(sep=" ")
        )


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
