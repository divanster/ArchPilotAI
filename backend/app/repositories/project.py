import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.architecture import ArchitectureProposal
from app.models.project import Project
from app.models.requirement import ExtractionRun, Requirement


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
        # Children first: requirements reference documents and extraction runs.
        self._session.execute(
            delete(ArchitectureProposal).where(ArchitectureProposal.project_id == project.id)
        )
        self._session.execute(delete(Requirement).where(Requirement.project_id == project.id))
        self._session.execute(delete(ExtractionRun).where(ExtractionRun.project_id == project.id))
        self._session.delete(project)
        self._session.commit()
