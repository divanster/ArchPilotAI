import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.architecture import (
    ArchitectureContent,
    ProposalStatus,
    content_to_json,
    referenced_requirement_ids,
    verify_references,
)
from app.models.architecture import ArchitectureProposal
from app.repositories.architecture import ArchitectureRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.project import ProjectNotFoundError


class ProposalNotFoundError(Exception):
    def __init__(self, proposal_id: uuid.UUID) -> None:
        super().__init__(f"Architecture proposal {proposal_id} not found")


class ProposalNotEditableError(Exception):
    pass


class StaleProposalError(Exception):
    pass


@dataclass(frozen=True)
class ProposalChange:
    title: str | None = None
    summary: str | None = None
    content: ArchitectureContent | None = None
    status: ProposalStatus | None = None  # only approved / rejected


class ArchitectureService:
    def __init__(
        self,
        architecture: ArchitectureRepository,
        projects: ProjectRepository,
        requirements: RequirementRepository,
    ) -> None:
        self._architecture = architecture
        self._projects = projects
        self._requirements = requirements

    def _require_project(self, project_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)

    def _confirmed_ids(self, project_id: uuid.UUID) -> set[uuid.UUID]:
        return {r.id for r in self._requirements.list(project_id, "confirmed")}

    def list(
        self, project_id: uuid.UUID, status: ProposalStatus | None = None
    ) -> Sequence[ArchitectureProposal]:
        self._require_project(project_id)
        return self._architecture.list(project_id, status.value if status else None)

    def get(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> ArchitectureProposal:
        self._require_project(project_id)
        proposal = self._architecture.get(project_id, proposal_id)
        if proposal is None:
            raise ProposalNotFoundError(proposal_id)
        return proposal

    def stale_references(
        self, project_id: uuid.UUID, proposals: Sequence[ArchitectureProposal]
    ) -> dict[uuid.UUID, Sequence[uuid.UUID]]:
        """Cited requirements that are no longer confirmed (edited or rejected since)."""
        confirmed = self._confirmed_ids(project_id)
        stale: dict[uuid.UUID, Sequence[uuid.UUID]] = {}
        for proposal in proposals:
            cited = referenced_requirement_ids(ArchitectureContent.model_validate(proposal.content))
            stale[proposal.id] = sorted(cited - confirmed, key=str)
        return stale

    def update(
        self, project_id: uuid.UUID, proposal_id: uuid.UUID, change: ProposalChange
    ) -> ArchitectureProposal:
        proposal = self.get(project_id, proposal_id)
        if proposal.status != ProposalStatus.DRAFT.value:
            raise ProposalNotEditableError(
                f"Only a draft proposal can be changed; this one is {proposal.status}. "
                "Generate a new version instead."
            )
        confirmed = self._confirmed_ids(project_id)
        if change.content is not None:
            verify_references(change.content, confirmed, require_complete=False)
        if change.status is ProposalStatus.APPROVED:
            cited = referenced_requirement_ids(ArchitectureContent.model_validate(proposal.content))
            if not cited <= confirmed:
                raise StaleProposalError(
                    "The proposal cites requirements that are no longer confirmed; "
                    "it cannot be approved"
                )

        if change.title is not None:
            proposal.title = change.title
        if change.summary is not None:
            proposal.summary = change.summary
        if change.content is not None:
            proposal.content = content_to_json(change.content)
        if change.status is not None:
            proposal.status = change.status.value
        return self._architecture.save(proposal)
