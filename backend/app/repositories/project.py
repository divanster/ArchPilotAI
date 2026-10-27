import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, project: Project) -> Project:
        self._session.add(project)
        self._session.commit()
        self._session.refresh(project)
        return project

    def get(self, project_id: uuid.UUID) -> Project | None:
        return self._session.get(Project, project_id)

    def list(self) -> Sequence[Project]:
        stmt = select(Project).order_by(Project.created_at.desc(), Project.id)
        return self._session.scalars(stmt).all()

    def save(self, project: Project) -> Project:
        self._session.commit()
        self._session.refresh(project)
        return project

    def delete(self, project: Project) -> None:
        self._session.delete(project)
        self._session.commit()
