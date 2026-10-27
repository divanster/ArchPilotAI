from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_session
from app.main import app
from app.models import project as _project  # noqa: F401
from app.models.base import Base


@pytest.fixture
def engine() -> Iterator[Engine]:
    # Isolated in-memory database; never touches a configured/external database.
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
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
