import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.domain.architecture import (
    ArchitectureContent,
    FailureCategory,
    InvalidArchitectureOutputError,
    ProposalStatus,
    cited_passage_ids,
    content_to_json,
    referenced_requirement_ids,
    verify_references,
)
from app.domain.semantic_lint import LintConfig
from app.models.architecture import ArchitectureProposal
from app.repositories.architecture import ArchitectureRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture_lint import compute_lint
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)


class ProposalNotFoundError(Exception):
    def __init__(self, proposal_id: uuid.UUID) -> None:
        super().__init__(f"Architecture proposal {proposal_id} not found")


class ProposalNotEditableError(Exception):
    pass


class StaleProposalError(Exception):
    pass


class NothingToReviseError(Exception):
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
        lint_config: LintConfig | None = None,
        on_approved: Callable[[uuid.UUID, uuid.UUID], object] | None = None,
    ) -> None:
        self._on_approved = on_approved
        self._lint_config = lint_config
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
        new_content: ArchitectureContent | None = None
        if change.content is not None:
            # Provenance is never editable: keep the stored sources and only allow citing them.
            stored = ArchitectureContent.model_validate(proposal.content)
            known = {p.chunk_id for p in stored.source_passages}
            if not cited_passage_ids(change.content) <= known:
                raise InvalidArchitectureOutputError(
                    "An item cites a passage that is not a recorded source of this proposal",
                    category=FailureCategory.REFERENCE,
                )
            new_content = change.content.model_copy(
                update={"source_passages": stored.source_passages, "retrieval": stored.retrieval}
            )
            verify_references(new_content, confirmed, require_complete=False)
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
        if new_content is not None:
            proposal.content = content_to_json(new_content)
        if change.status is not None:
            proposal.status = change.status.value
        if change.status is None:
            # Findings follow the text a reviewer now sees; the content itself is untouched.
            proposal.lint_findings = compute_lint(
                self._requirements,
                project_id,
                proposal.title,
                proposal.summary,
                new_content or ArchitectureContent.model_validate(proposal.content),
                self._lint_config,
            )
        saved = self._architecture.save(proposal)
        if change.status is ProposalStatus.APPROVED and self._on_approved is not None:
            # Derived data: a failed rebuild must never undo or block an approval.
            try:
                self._on_approved(project_id, proposal_id)
            except Exception:
                logger.exception("Graph rebuild after approval failed for %s", proposal_id)
        return saved
