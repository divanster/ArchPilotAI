from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.db import get_database, make_session_factory
from app.llm.exceptions import LLMConfigurationError
from app.llm.factory import create_provider
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.services.ai_service import AIService
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


@lru_cache
def _build_ai_service() -> AIService:
    settings = get_settings()
    return AIService(
        create_provider(settings),
        max_prompt_chars=settings.ai_max_prompt_chars,
        max_output_tokens=settings.ai_max_output_tokens,
        min_interval_seconds=settings.ai_min_interval_seconds,
    )


def get_ai_service() -> AIService:
    try:
        return _build_ai_service()
    except LLMConfigurationError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.public_message) from exc


AIServiceDep = Annotated[AIService, Depends(get_ai_service)]
