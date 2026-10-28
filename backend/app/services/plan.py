import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.architecture import ArchitectureContent, ProposalStatus
from app.domain.graph import EdgeKind, NodeKind
from app.domain.plan import build_plan
from app.models.plan import ImplementationPlan, PlanTask
from app.repositories.architecture import ArchitectureRepository
from app.repositories.graph import GraphRepository
from app.repositories.plan import PlanRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.project import ProjectNotFoundError


class PlanNotFoundError(Exception):
    def __init__(self, plan_id: uuid.UUID) -> None:
        super().__init__(f"Implementation plan {plan_id} not found")


class NoApprovedArchitectureError(Exception):
    def __init__(self) -> None:
        super().__init__("The project has no approved architecture; approve a proposal first")


class GraphNotBuiltError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "The traceability graph of the approved architecture is not built; rebuild it first"
        )


class PlanNotDraftError(Exception):
    pass


@dataclass(frozen=True)
class PlanDetail:
    plan: ImplementationPlan
    tasks: Sequence[PlanTask]


class PlanService:
    def __init__(
        self,
        plans: PlanRepository,
        architecture: ArchitectureRepository,
        graph: GraphRepository,
        projects: ProjectRepository,
        requirements: RequirementRepository,
    ) -> None:
        self._plans = plans
        self._architecture = architecture
        self._graph = graph
        self._projects = projects
        self._requirements = requirements

    def _require_project(self, project_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)

    def build(self, project_id: uuid.UUID) -> PlanDetail:
        """Always creates a new draft version; an existing plan is never edited."""
        self._require_project(project_id)
        approved = self._architecture.list(project_id, ProposalStatus.APPROVED.value)
        if not approved:
            raise NoApprovedArchitectureError
        proposal = approved[0]  # newest version first
        nodes = {n.id: n for n in self._graph.nodes(project_id, proposal.id)}
        if not nodes:
            raise GraphNotBuiltError
        dependencies = [
            (nodes[e.source_id].source_ref, nodes[e.target_id].source_ref)
            for e in self._graph.edges(project_id, proposal.id)
            if e.kind == EdgeKind.DEPENDS_ON.value
            and nodes[e.source_id].kind == NodeKind.COMPONENT.value
            and nodes[e.target_id].kind == NodeKind.COMPONENT.value
        ]
        built_at = self._graph.built_at(project_id, proposal.id)
        confirmed = [r.id for r in self._requirements.list(project_id, "confirmed")]
        spec = build_plan(
            ArchitectureContent.model_validate(proposal.content), confirmed, dependencies
        )
        plan = self._plans.create(
            project_id, proposal.id, spec, built_at if isinstance(built_at, datetime) else None
        )
        return PlanDetail(plan, self._plans.tasks(project_id, plan.id))

    def list(
        self, project_id: uuid.UUID
    ) -> tuple[Sequence[ImplementationPlan], Counter[uuid.UUID]]:
        self._require_project(project_id)
        return self._plans.list(project_id), self._plans.task_counts(project_id)

    def get(self, project_id: uuid.UUID, plan_id: uuid.UUID) -> PlanDetail:
        self._require_project(project_id)
        plan = self._plans.get(project_id, plan_id)
        if plan is None:
            raise PlanNotFoundError(plan_id)
        return PlanDetail(plan, self._plans.tasks(project_id, plan_id))

    def get_version(self, project_id: uuid.UUID, version: int | None) -> PlanDetail | None:
        """The given version, or the newest approved plan; None when there is no match."""
        self._require_project(project_id)
        plans = self._plans.list(project_id, None if version is not None else "approved")
        matches = [p for p in plans if version is None or p.version == version]
        if not matches:
            return None
        return PlanDetail(matches[0], self._plans.tasks(project_id, matches[0].id))

    def decide(self, project_id: uuid.UUID, plan_id: uuid.UUID, status: str) -> PlanDetail:
        detail = self.get(project_id, plan_id)
        if detail.plan.status != "draft":
            raise PlanNotDraftError(
                f"Only a draft plan can be approved or rejected; this one is {detail.plan.status}. "
                "Build a new version instead."
            )
        return PlanDetail(self._plans.set_status(detail.plan, status), detail.tasks)
