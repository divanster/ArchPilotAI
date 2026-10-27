import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from alembic import command
from app.core import db as db_module
from app.core.config import Settings, get_settings
from app.core.db import Database, DatabaseUnavailableError, select_database
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent
SECRET = "super-secret-test-value"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "local",
        "database_mode": "auto",
        "db_host": "127.0.0.1",
        "db_port": 1,  # nothing listens here: PostgreSQL is "unavailable"
        "db_password": SECRET,
        "db_connect_timeout_seconds": 1,
        "sqlite_path": str(tmp_path / "data" / "archpilot.db"),
        "require_postgres": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _clean_selection() -> Iterator[None]:
    db_module.reset_database()
    get_settings.cache_clear()
    yield
    db_module.reset_database()
    get_settings.cache_clear()


def test_explicit_sqlite_uses_persistent_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database_mode="sqlite")
    database = select_database(settings)
    assert (database.kind, database.fallback_active) == ("sqlite", False)
    assert database.engine.url.database == (tmp_path / "data" / "archpilot.db").as_posix()
    with database.engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
    database.engine.dispose()
    assert (tmp_path / "data" / "archpilot.db").is_file()
    assert "t" in inspect(select_database(settings).engine).get_table_names()


def test_in_memory_sqlite_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        make_settings(tmp_path, database_mode="sqlite", sqlite_path=":memory:")


def test_explicit_postgres_fails_clearly_without_fallback(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, database_mode="postgres")
    with pytest.raises(DatabaseUnavailableError) as info:
        select_database(settings)
    assert "PostgreSQL is required" in str(info.value)
    assert SECRET not in str(info.value)
    assert not (tmp_path / "data").exists()


def test_auto_uses_postgres_when_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stand_in = create_engine(f"sqlite:///{tmp_path / 'pg_stand_in.db'}")
    monkeypatch.setattr(db_module, "_make_postgres_engine", lambda _s: stand_in)
    database = select_database(make_settings(tmp_path))
    assert database.engine is stand_in
    assert (database.kind, database.fallback_active) == ("postgresql", False)
    assert not (tmp_path / "data").exists()


def test_auto_uses_real_postgres_when_available(tmp_path: Path, postgres_url: str) -> None:
    url = make_url(postgres_url)
    settings = make_settings(
        tmp_path,
        db_host=str(url.host),
        db_port=url.port or 5432,
        db_name=str(url.database),
        db_user=str(url.username),
        db_password=str(url.password),
    )
    database = select_database(settings)
    try:
        assert (database.kind, database.fallback_active) == ("postgresql", False)
    finally:
        database.engine.dispose()


def test_auto_falls_back_with_warning_and_no_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.db"):
        database = select_database(make_settings(tmp_path))
    assert (database.kind, database.fallback_active) == ("sqlite", True)
    assert "FALLBACK" in caplog.text
    assert SECRET not in caplog.text
    assert "postgresql+psycopg" not in caplog.text


@pytest.mark.parametrize("environment", ["production", "ci", "test"])
def test_auto_is_prohibited_outside_local(tmp_path: Path, environment: str) -> None:
    with pytest.raises(ValidationError, match="only allowed when environment=local"):
        make_settings(tmp_path, environment=environment)


def test_production_requires_postgres(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="production requires"):
        make_settings(tmp_path, environment="production", database_mode="sqlite")
    make_settings(tmp_path, environment="production", database_mode="postgres")


def test_require_postgres_prohibits_sqlite_and_auto(tmp_path: Path) -> None:
    for mode in ("sqlite", "auto"):
        with pytest.raises(ValidationError, match="PostgreSQL is required"):
            make_settings(tmp_path, database_mode=mode, require_postgres=True)


def test_require_postgres_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHPILOT_REQUIRE_POSTGRES", "1")
    monkeypatch.setenv("ARCHPILOT_DATABASE_MODE", "auto")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_selection_is_made_once_and_never_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = create_engine(f"sqlite:///{tmp_path / 'pg_stand_in.db'}")
    monkeypatch.setattr(db_module, "_make_postgres_engine", lambda _s: stand_in)
    settings = make_settings(tmp_path)
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)

    first = db_module.get_database()
    assert first.kind == "postgresql"

    # PostgreSQL "goes away" after startup: queries fail, but the choice is final.
    def unreachable(_s: Settings) -> Engine:
        raise OperationalError("SELECT 1", {}, Exception("down"))

    monkeypatch.setattr(db_module, "_make_postgres_engine", unreachable)
    with pytest.raises(OperationalError), first.engine.connect() as conn:
        conn.execute(text("SELECT * FROM missing_table"))  # failed query is reported, not retried
    again = db_module.get_database()
    assert again is first
    assert again.kind == "postgresql"
    assert not (tmp_path / "data").exists()


def test_health_exposes_only_database_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = Database(create_engine("sqlite://"), "sqlite", True)
    monkeypatch.setattr(db_module, "_selected", fallback)
    body = TestClient(create_app()).get("/health").json()
    assert body["database"] == "sqlite"
    assert body["database_fallback"] is True
    assert SECRET not in str(body)
    assert "://" not in str(body)


def test_lifespan_selects_database_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHPILOT_REQUIRE_POSTGRES", "0")
    monkeypatch.setenv("ARCHPILOT_DATABASE_MODE", "auto")
    monkeypatch.setenv("ARCHPILOT_ENVIRONMENT", "local")
    monkeypatch.setenv("ARCHPILOT_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("ARCHPILOT_DB_PORT", "1")
    monkeypatch.setenv("ARCHPILOT_DB_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ARCHPILOT_SQLITE_PATH", str(tmp_path / "d" / "app.db"))
    with TestClient(create_app()) as client:
        body = client.get("/health").json()
    assert body["database"] == "sqlite"
    assert body["database_fallback"] is True


def test_startup_fails_when_postgres_required_and_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCHPILOT_DATABASE_MODE", "postgres")
    monkeypatch.setenv("ARCHPILOT_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("ARCHPILOT_DB_PORT", "1")
    monkeypatch.setenv("ARCHPILOT_DB_CONNECT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("ARCHPILOT_SQLITE_PATH", str(tmp_path / "d" / "app.db"))
    with pytest.raises(DatabaseUnavailableError), TestClient(create_app()):
        pass
    assert not (tmp_path / "d").exists()


def test_alembic_uses_selected_database_and_refuses_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "migrated.db"
    monkeypatch.setenv("ARCHPILOT_REQUIRE_POSTGRES", "0")
    monkeypatch.setenv("ARCHPILOT_DATABASE_MODE", "sqlite")
    monkeypatch.setenv("ARCHPILOT_SQLITE_PATH", str(path))
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    assert {"projects", "documents"} <= set(
        inspect(create_engine(f"sqlite:///{path}")).get_table_names()
    )

    monkeypatch.setenv("ARCHPILOT_DATABASE_MODE", "auto")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="explicit database"):
        _ = get_settings().migration_url
