import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

DatabaseKind = Literal["sqlite", "postgresql"]


class DatabaseUnavailableError(RuntimeError):
    """Required PostgreSQL could not be reached at startup."""


@dataclass(frozen=True)
class Database:
    """The database chosen for this process. Immutable: selected once, never switched."""

    engine: Engine
    kind: DatabaseKind
    fallback_active: bool


def _make_postgres_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.db_connect_timeout_seconds},
    )


def _make_sqlite_engine(settings: Settings) -> Engine:
    path = Path(settings.sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.sqlite_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _probe(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def select_database(settings: Settings) -> Database:
    """Choose the engine. Only a failed PostgreSQL *connection* in `auto` mode falls back."""
    mode = settings.database_mode
    if mode == "sqlite":
        logger.info("database selected: sqlite")
        return Database(_make_sqlite_engine(settings), "sqlite", False)

    engine = _make_postgres_engine(settings)
    try:
        _probe(engine)
    except OperationalError as exc:
        engine.dispose()
        # Log the exception type only: messages can echo connection details.
        reason = type(exc.orig).__name__ if exc.orig else type(exc).__name__
        if mode == "postgres":
            raise DatabaseUnavailableError(
                "PostgreSQL is required but unreachable "
                f"({settings.db_host}:{settings.db_port}, {reason}). "
                "Start it, or set ARCHPILOT_DATABASE_MODE=sqlite for local development."
            ) from None
        logger.warning(
            "PostgreSQL unavailable (%s); DEVELOPMENT FALLBACK to local SQLite for this process. "
            "SQLite and PostgreSQL data are separate.",
            reason,
        )
        return Database(_make_sqlite_engine(settings), "sqlite", True)
    logger.info("database selected: postgresql")
    return Database(engine, "postgresql", False)


_lock = threading.Lock()
_selected: Database | None = None


def get_database() -> Database:
    """Select once per process (thread-safe); later calls always return the same choice."""
    global _selected
    with _lock:
        if _selected is None:
            _selected = select_database(get_settings())
        return _selected


def peek_database() -> Database | None:
    """The already-selected database, or None. Never triggers selection."""
    return _selected


def reset_database() -> None:
    """Dispose and forget the selection (tests and process shutdown only)."""
    global _selected
    with _lock:
        if _selected is not None:
            _selected.engine.dispose()
        _selected = None


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
