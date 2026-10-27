from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import make_engine, make_session_factory
from app.repositories.project import ProjectRepository
from app.services.project import ProjectService


@lru_cache
def get_engine() -> Engine:
    return make_engine()


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return make_session_factory(get_engine())


def get_session() -> Iterator[Session]:
    with get_session_factory()() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_project_service(session: SessionDep) -> ProjectService:
    return ProjectService(ProjectRepository(session))


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
