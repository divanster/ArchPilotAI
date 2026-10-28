import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.domain.architecture import ArchitectureContent, ProposalStatus
from app.domain.plan import SOURCE_MANUAL, build_plan, find_path
from app.models.plan import ImplementationPlan, PlanDependencyEdit, PlanTask
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


class PlanEditError(Exception):
    """A manual dependency edit that is invalid (other plan, self, duplicate, cycle, ...)."""


@dataclass(frozen=True)
class PlanDetail:
    plan: ImplementationPlan
    tasks: Sequence[PlanTask]
    edits: Sequence[PlanDependencyEdit] = ()
    # Manual edits of the previous version; a rebuild does not carry them over.
    previous_version: int | None = None
    previous_edits: Sequence[PlanDependencyEdit] = ()

    def manual_reason(self, task: PlanTask, key: str) -> PlanDependencyEdit | None:
        """The edit that added the manual dependency `task -> key`, if it is still in place."""
        if task.source_of(key) != SOURCE_MANUAL:
            return None
        added = [
            e
            for e in self.edits
            if e.action == "add" and e.task_key == task.key and e.depends_on_key == key
        ]
        return added[-1] if added else None


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
        nodes = self._graph.nodes(project_id, proposal.id)
        if not nodes:
            raise GraphNotBuiltError
        built_at = self._graph.built_at(project_id, proposal.id)
        confirmed = [r.id for r in self._requirements.list(project_id, "confirmed")]
        spec = build_plan(ArchitectureContent.model_validate(proposal.content), confirmed)
        plan = self._plans.create(
            project_id, proposal.id, spec, built_at if isinstance(built_at, datetime) else None
        )
        return self._detail(project_id, plan)

    def _detail(self, project_id: uuid.UUID, plan: ImplementationPlan) -> PlanDetail:
        earlier = [p for p in self._plans.list(project_id) if p.version < plan.version]
        previous = earlier[0] if earlier else None  # newest first
        return PlanDetail(
            plan,
            self._plans.tasks(project_id, plan.id),
            self._plans.edits(project_id, plan.id),
            previous.version if previous else None,
            self._plans.edits(project_id, previous.id) if previous else (),
        )

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
        return self._detail(project_id, plan)

    def get_version(self, project_id: uuid.UUID, version: int | None) -> PlanDetail | None:
        """The given version, or the newest approved plan; None when there is no match."""
        self._require_project(project_id)
        plans = self._plans.list(project_id, None if version is not None else "approved")
        matches = [p for p in plans if version is None or p.version == version]
        if not matches:
            return None
        return self._detail(project_id, matches[0])

    def decide(self, project_id: uuid.UUID, plan_id: uuid.UUID, status: str) -> PlanDetail:
        detail = self.get(project_id, plan_id)
        self._require_draft(detail.plan, "approved or rejected")
        self._plans.set_status(detail.plan, status)
        return self._detail(project_id, detail.plan)

    @staticmethod
    def _require_draft(plan: ImplementationPlan, what: str) -> None:
        if plan.status != "draft":
            raise PlanNotDraftError(
                f"Only a draft plan can be {what}; this one is {plan.status}. "
                "Build a new version instead."
            )

    def add_dependency(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_id: uuid.UUID,
        depends_on_id: uuid.UUID,
        reason: str,
        author: str,
    ) -> PlanDetail:
        task, target, detail = self._edit_context(
            project_id, plan_id, task_id, depends_on_id, reason, author
        )
        if target.key in task.depends_on:
            raise PlanEditError(f"{task.key} already depends on {target.key}")
        graph = {t.key: list(t.depends_on) for t in detail.tasks}
        path = find_path(graph, target.key, task.key)
        if path is not None:
            raise PlanEditError(
                f"Adding {task.key} -> {target.key} would create a dependency cycle: "
                + " -> ".join([task.key, *path])
            )
        task.depends_on = sorted([*task.depends_on, target.key])
        task.dependency_sources = {**task.dependency_sources, target.key: SOURCE_MANUAL}
        return self._record(project_id, detail, "add", task, target, reason.strip(), author.strip())

    def remove_dependency(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_id: uuid.UUID,
        depends_on_id: uuid.UUID,
        reason: str,
        author: str,
    ) -> PlanDetail:
        task, target, detail = self._edit_context(
            project_id, plan_id, task_id, depends_on_id, reason, author
        )
        if target.key not in task.depends_on:
            raise PlanEditError(f"{task.key} does not depend on {target.key}")
        task.depends_on = [k for k in task.depends_on if k != target.key]
        task.dependency_sources = {
            k: v for k, v in task.dependency_sources.items() if k != target.key
        }
        return self._record(
            project_id, detail, "remove", task, target, reason.strip(), author.strip()
        )

    def _edit_context(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_id: uuid.UUID,
        depends_on_id: uuid.UUID,
        reason: str,
        author: str,
    ) -> tuple[PlanTask, PlanTask, PlanDetail]:
        detail = self.get(project_id, plan_id)
        self._require_draft(detail.plan, "edited")
        if not reason.strip():
            raise PlanEditError("A reason is required")
        if not author.strip():
            raise PlanEditError("An author is required")
        by_id = {t.id: t for t in detail.tasks}
        if task_id not in by_id or depends_on_id not in by_id:
            raise PlanEditError("Both tasks must belong to this plan")
        if task_id == depends_on_id:
            raise PlanEditError("A task cannot depend on itself")
        return by_id[task_id], by_id[depends_on_id], detail

    def _record(
        self,
        project_id: uuid.UUID,
        detail: PlanDetail,
        action: str,
        task: PlanTask,
        target: PlanTask,
        reason: str,
        author: str,
    ) -> PlanDetail:
        edit = PlanDependencyEdit(
            id=uuid.uuid4(),
            plan_id=detail.plan.id,
            project_id=project_id,
            action=action,
            task_key=task.key,
            depends_on_key=target.key,
            reason=reason,
            author=author,
        )
        self._plans.apply_edit(detail.plan, detail.tasks, edit)
        return self._detail(project_id, detail.plan)
