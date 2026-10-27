from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.schemas import ProjectCreate, ProjectUpdate
from app.domain.project import ProjectStatus
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.services.document import DocumentService
from app.services.project import ProjectService


def test_data_persists_across_engines(database_url: str) -> None:
    engine = create_engine(database_url)
    with sessionmaker(bind=engine)() as session:
        service = ProjectService(ProjectRepository(session))
        project_id = service.create(ProjectCreate(name="Persisted")).id
        service.update(project_id, ProjectUpdate(status=ProjectStatus.ACTIVE))
    engine.dispose()

    reopened = create_engine(database_url)
    with sessionmaker(bind=reopened)() as session:
        loaded = ProjectService(ProjectRepository(session)).get(project_id)
        assert loaded.name == "Persisted"
        assert loaded.status is ProjectStatus.ACTIVE
        assert loaded.updated_at >= loaded.created_at
    reopened.dispose()


def test_delete_removes_row(database_url: str) -> None:
    engine = create_engine(database_url)
    with sessionmaker(bind=engine)() as session:
        service = ProjectService(ProjectRepository(session))
        project = service.create(ProjectCreate(name="Temp"))
        service.delete(project.id)
        assert service.list() == []
    engine.dispose()


def test_documents_persist_and_cascade_with_project(database_url: str) -> None:
    engine = create_engine(database_url)
    with sessionmaker(bind=engine)() as session:
        projects = ProjectService(ProjectRepository(session))
        project_id = projects.create(ProjectCreate(name="With docs")).id
        documents = DocumentService(DocumentRepository(session), ProjectRepository(session), 1024)
        doc_id = documents.upload(project_id, "a.txt", "text/plain", b"synthetic text").id
    engine.dispose()

    reopened = create_engine(database_url)
    with sessionmaker(bind=reopened)() as session:
        documents = DocumentService(DocumentRepository(session), ProjectRepository(session), 1024)
        loaded = documents.get(project_id, doc_id)
        assert loaded.extracted_text == "synthetic text"
        ProjectService(ProjectRepository(session)).delete(project_id)
        assert session.execute(text("SELECT count(*) FROM documents")).scalar_one() == 0
    reopened.dispose()
