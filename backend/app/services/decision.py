import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.domain.architecture import ArchitectureContent
from app.domain.decisions import (
    AREAS,
    area_for_task,
    decisions_block,
    stack_suggestions,
    task_flags,
)
from app.domain.plan import AI_RECOMMENDATION_FLAG
from app.models.decision import ProjectDecision
from app.models.plan import PlanTask
from app.repositories.architecture import ArchitectureRepository
from app.repositories.decision import DecisionRepository
from app.repositories.project import ProjectRepository
from app.services.project import ProjectNotFoundError


class DecisionInputError(Exception):
    pass


class DecisionConflictError(Exception):
    pass


@dataclass(frozen=True)
class DecisionsView:
    current: Sequence[ProjectDecision]
    history: Sequence[ProjectDecision]
    suggestions: dict[str, dict[str, str]]


class DecisionService:
    def __init__(
        self,
        decisions: DecisionRepository,
        projects: ProjectRepository,
        architecture: ArchitectureRepository,
    ) -> None:
        self._decisions = decisions
        self._projects = projects
        self._architecture = architecture

    def _require_project(self, project_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)

    def current(self, project_id: uuid.UUID) -> Sequence[ProjectDecision]:
        return self._decisions.current(project_id)

    def view(self, project_id: uuid.UUID) -> DecisionsView:
        self._require_project(project_id)
        proposals = self._architecture.list(project_id, "approved")
        suggestions: dict[str, dict[str, str]] = {}
        if proposals:
            stack = ArchitectureContent.model_validate(proposals[0].content).recommended_stack
            suggestions = stack_suggestions(
                {"layer": s.layer, "technology": s.technology} for s in stack
            )
        return DecisionsView(
            self._decisions.current(project_id), self._decisions.history(project_id), suggestions
        )

    def decide(
        self, project_id: uuid.UUID, area: str, choice: str, reason: str, decided_by: str
    ) -> ProjectDecision:
        """Records a decision; an existing one for the area is superseded, never edited."""
        self._require_project(project_id)
        if area not in AREAS:
            raise DecisionInputError(f"area must be one of: {', '.join(AREAS)}")
        values = [v.strip() for v in (choice, reason, decided_by)]
        if not all(values):
            raise DecisionInputError("choice, reason and name are required")
        try:
            return self._decisions.replace(
                ProjectDecision(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    area=area,
                    choice=values[0],
                    reason=values[1],
                    decided_by=values[2],
                )
            )
        except IntegrityError as exc:
            raise DecisionConflictError("The decision was changed concurrently; retry") from exc

    # ---- context for generation and agent runs ----------------------------------------------

    def _recommended(self, project_id: uuid.UUID, proposal_id: uuid.UUID, area: str) -> str | None:
        proposal = self._architecture.get(project_id, proposal_id)
        if proposal is None:
            return None
        stack = ArchitectureContent.model_validate(proposal.content).recommended_stack
        found = stack_suggestions({"layer": s.layer, "technology": s.technology} for s in stack)
        return found.get(area, {}).get("choice")

    def decision_for_task(self, project_id: uuid.UUID, task: PlanTask) -> ProjectDecision | None:
        area = area_for_task(task.kind, task.title)
        return self._decisions.current_for_area(project_id, area) if area else None

    def task_context(
        self, project_id: uuid.UUID, proposal_id: uuid.UUID, task: PlanTask
    ) -> tuple[dict[str, Any], list[str]]:
        """The labelled current-decisions block and the task flags, confirmed where decided."""
        block = decisions_block(self._decisions.current(project_id))
        decision = self.decision_for_task(project_id, task)
        recommended = (
            self._recommended(project_id, proposal_id, decision.area)
            if decision is not None and AI_RECOMMENDATION_FLAG in task.flags
            else None
        )
        return block, task_flags(list(task.flags), decision, recommended)
