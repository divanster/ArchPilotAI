import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.api.deps import get_session
from app.main import app
from app.models import project as _project  # noqa: F401
from app.models.base import Base

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Process environment beats backend/.env: tests must never reach a real LLM provider,
# whatever a developer keeps in their local .env.
for _name in (
    "ARCHPILOT_WORKBENCH_API_KEY",
    "ARCHPILOT_WORKBENCH_ENDPOINT",
    "ARCHPILOT_WORKBENCH_DEPLOYMENT",
    "ARCHPILOT_WORKBENCH_API_VERSION",
    "ARCHPILOT_WORKBENCH_REGION",
    "ARCHPILOT_WORKBENCH_CHARGE_CODE",
    "ARCHPILOT_WORKBENCH_PROXY",
):
    os.environ[_name] = ""
os.environ["ARCHPILOT_LLM_PROVIDER"] = "fake"
os.environ["ARCHPILOT_EXTRACTION_ALLOW_REAL_PROVIDER"] = "false"
TEST_DB_URL_ENV = "ARCHPILOT_TEST_DATABASE_URL"
REQUIRE_PG_ENV = "ARCHPILOT_REQUIRE_POSTGRES"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """Empty, local, test-only PostgreSQL database migrated to head via Alembic."""
    url = os.environ.get(TEST_DB_URL_ENV)
    if not url:
        if os.environ.get(REQUIRE_PG_ENV) == "1":
            pytest.fail(f"{TEST_DB_URL_ENV} must be set when {REQUIRE_PG_ENV}=1")
        pytest.skip(f"{TEST_DB_URL_ENV} not set; PostgreSQL tests skipped")

    parsed = make_url(url)
    # Safety guards: never run against a non-local or non-test database.
    assert parsed.get_backend_name() == "postgresql", "test URL must be PostgreSQL"
    assert parsed.host in LOCAL_HOSTS, f"refusing non-local test database host: {parsed.host}"
    assert (parsed.database or "").endswith("_test"), "test database name must end with '_test'"

    engine = create_engine(url)
    try:
        existing = inspect(engine).get_table_names()
        assert existing == [], f"test database is not empty: {existing}"
        command.upgrade(alembic_config(url), "head")
    finally:
        engine.dispose()
    yield url


@pytest.fixture(params=["sqlite", "postgresql"])
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        url = f"sqlite+pysqlite:///{tmp_path / 'test.db'}"
        engine = create_engine(url)
        Base.metadata.create_all(engine)
        engine.dispose()
        return url

    url = str(request.getfixturevalue("postgres_url"))
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE chunk_embeddings, document_chunks, architecture_proposals, "
                "requirements, "
                "extraction_runs, documents, projects"
            )
        )
    engine.dispose()
    return url


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    kwargs = {"connect_args": {"check_same_thread": False}} if "sqlite" in database_url else {}
    eng = create_engine(database_url, **kwargs)
    if eng.dialect.name == "sqlite":

        @event.listens_for(eng, "connect")
        def _foreign_keys(dbapi_connection: Any, _record: Any) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override() -> Iterator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    yield TestClient(app)
    app.dependency_overrides.clear()
