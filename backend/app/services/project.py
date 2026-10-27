import uuid
from collections.abc import Sequence

from app.api.schemas import ProjectCreate, ProjectUpdate
from app.models.project import Project
from app.repositories.project import ProjectRepository


class ProjectNotFoundError(Exception):
    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__(f"Project {project_id} not found")
        self.project_id = project_id


class ProjectService:
    def __init__(self, repository: ProjectRepository) -> None:
        self._repo = repository

    def create(self, data: ProjectCreate) -> Project:
        return self._repo.add(
            Project(name=data.name, description=data.description, status=data.status)
        )

    def list(self) -> Sequence[Project]:
        return self._repo.list()

    def get(self, project_id: uuid.UUID) -> Project:
        project = self._repo.get(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    def update(self, project_id: uuid.UUID, data: ProjectUpdate) -> Project:
        project = self.get(project_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(project, field, value)
        return self._repo.save(project)

    def delete(self, project_id: uuid.UUID) -> None:
        self._repo.delete(self.get(project_id))
