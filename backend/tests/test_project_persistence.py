from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.schemas import ProjectCreate, ProjectUpdate
from app.domain.project import ProjectStatus
from app.models import project as _project  # noqa: F401
from app.models.base import Base
from app.repositories.project import ProjectRepository
from app.services.project import ProjectService


def test_data_persists_across_engines(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'persist.db'}"

    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        service = ProjectService(ProjectRepository(session))
        project = service.create(ProjectCreate(name="Persisted"))
        project_id = project.id
        service.update(project_id, ProjectUpdate(status=ProjectStatus.ACTIVE))
    engine.dispose()

    reopened = create_engine(url)
    with sessionmaker(bind=reopened)() as session:
        loaded = ProjectService(ProjectRepository(session)).get(project_id)
        assert loaded.name == "Persisted"
        assert loaded.status is ProjectStatus.ACTIVE
        assert loaded.updated_at >= loaded.created_at
        raw = session.execute(text("select status from projects")).scalar_one()
        assert raw == "active"
    reopened.dispose()


def test_delete_removes_row(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'del.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        service = ProjectService(ProjectRepository(session))
        project = service.create(ProjectCreate(name="Temp"))
        service.delete(project.id)
        assert service.list() == []
    engine.dispose()
