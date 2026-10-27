import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.requirement import RequirementStatus
from app.models.requirement import Requirement
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.project import ProjectNotFoundError


class RequirementNotFoundError(Exception):
    def __init__(self, requirement_id: uuid.UUID) -> None:
        super().__init__(f"Requirement {requirement_id} not found")


class InvalidTransitionError(Exception):
    pass


@dataclass(frozen=True)
class RequirementChange:
    title: str | None = None
    description: str | None = None
    status: RequirementStatus | None = None  # only confirmed / rejected are accepted


class RequirementService:
    def __init__(self, requirements: RequirementRepository, projects: ProjectRepository) -> None:
        self._requirements = requirements
        self._projects = projects

    def _require_project(self, project_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)

    def list(
        self,
        project_id: uuid.UUID,
        status: RequirementStatus | None = None,
        document_id: uuid.UUID | None = None,
    ) -> Sequence[Requirement]:
        self._require_project(project_id)
        return self._requirements.list(project_id, status.value if status else None, document_id)

    def get(self, project_id: uuid.UUID, requirement_id: uuid.UUID) -> Requirement:
        self._require_project(project_id)
        requirement = self._requirements.get(project_id, requirement_id)
        if requirement is None:
            raise RequirementNotFoundError(requirement_id)
        return requirement

    def update(
        self, project_id: uuid.UUID, requirement_id: uuid.UUID, change: RequirementChange
    ) -> Requirement:
        requirement = self.get(project_id, requirement_id)
        if change.status not in (None, RequirementStatus.CONFIRMED, RequirementStatus.REJECTED):
            raise InvalidTransitionError("Status can only be set to confirmed or rejected")

        edited = (change.title is not None and change.title != requirement.title) or (
            change.description is not None and change.description != requirement.description
        )
        # Reviewed text changed, so any earlier review no longer applies.
        effective = RequirementStatus.PROPOSED.value if edited else requirement.status
        if change.status is not None and effective != RequirementStatus.PROPOSED.value:
            raise InvalidTransitionError(
                f"Only a proposed requirement can be {change.status.value}; this one is {effective}"
            )

        if change.title is not None:
            requirement.title = change.title
        if change.description is not None:
            requirement.description = change.description
        requirement.status = change.status.value if change.status else effective
        return self._requirements.save(requirement)
