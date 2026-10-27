from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import get_database, make_session_factory
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.services.document import DocumentService
from app.services.project import ProjectService


def get_engine() -> Engine:
    return get_database().engine


def get_session_factory() -> sessionmaker[Session]:
    return make_session_factory(get_engine())


def get_session() -> Iterator[Session]:
    with get_session_factory()() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_project_service(session: SessionDep) -> ProjectService:
    return ProjectService(ProjectRepository(session))


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]


def get_max_upload_bytes() -> int:
    return get_settings().max_upload_bytes


MaxUploadBytesDep = Annotated[int, Depends(get_max_upload_bytes)]


def get_document_service(session: SessionDep, max_bytes: MaxUploadBytesDep) -> DocumentService:
    return DocumentService(DocumentRepository(session), ProjectRepository(session), max_bytes)


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
