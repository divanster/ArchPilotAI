import heapq
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.plan import PlanSpec
from app.models.plan import ImplementationPlan, PlanDependencyEdit, PlanTask


def _topological(ranked: Sequence[PlanTask]) -> list[PlanTask]:
    """Stable order: dependencies first, otherwise the existing order (acyclic by validation)."""
    by_key = {t.key: t for t in ranked}
    remaining = {t.key: {d for d in t.depends_on if d in by_key} for t in ranked}
    position = {t.key: n for n, t in enumerate(ranked)}
    ready = [(position[k], k) for k, d in remaining.items() if not d]
    heapq.heapify(ready)
    out: list[PlanTask] = []
    while ready:
        _, key = heapq.heappop(ready)
        out.append(by_key[key])
        for other, deps in remaining.items():
            if key in deps:
                deps.discard(key)
                if not deps:
                    heapq.heappush(ready, (position[other], other))
    return out


class PlanRepository:
    """Every query is scoped by project."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, project_id: uuid.UUID, plan_id: uuid.UUID) -> ImplementationPlan | None:
        stmt = select(ImplementationPlan).where(
            ImplementationPlan.project_id == project_id, ImplementationPlan.id == plan_id
        )
        return self._session.scalars(stmt).one_or_none()

    def list(
        self, project_id: uuid.UUID, status: str | None = None
    ) -> Sequence[ImplementationPlan]:
        stmt = select(ImplementationPlan).where(ImplementationPlan.project_id == project_id)
        if status is not None:
            stmt = stmt.where(ImplementationPlan.status == status)
        return self._session.scalars(
            stmt.order_by(ImplementationPlan.version.desc(), ImplementationPlan.id)
        ).all()

    def tasks(self, project_id: uuid.UUID, plan_id: uuid.UUID) -> Sequence[PlanTask]:
        stmt = (
            select(PlanTask)
            .where(PlanTask.project_id == project_id, PlanTask.plan_id == plan_id)
            .order_by(PlanTask.order_index)
        )
        return self._session.scalars(stmt).all()

    def task_counts(self, project_id: uuid.UUID) -> Counter[uuid.UUID]:
        stmt = (
            select(PlanTask.plan_id, func.count())
            .where(PlanTask.project_id == project_id)
            .group_by(PlanTask.plan_id)
        )
        return Counter({plan_id: n for plan_id, n in self._session.execute(stmt).all()})

    def create(
        self,
        project_id: uuid.UUID,
        proposal_id: uuid.UUID,
        spec: PlanSpec,
        graph_built_at: datetime | None,
    ) -> ImplementationPlan:
        """Insert a new plan version with all its tasks in one transaction."""
        version = (
            self._session.scalar(
                select(func.max(ImplementationPlan.version)).where(
                    ImplementationPlan.project_id == project_id
                )
            )
            or 0
        ) + 1
        plan = ImplementationPlan(
            id=uuid.uuid4(),
            project_id=project_id,
            proposal_id=proposal_id,
            version=version,
            status="draft",
            source_graph_built_at=graph_built_at,
            unattached_questions=list(spec.unattached_questions),
            not_for_client=list(spec.not_for_client),
            dependency_count=spec.dependency_count,
            unmapped_node_count=len(spec.unmapped_nodes),
            warnings=list(spec.warnings),
        )
        try:
            self._session.add(plan)
            self._session.flush()
            self._session.add_all(
                PlanTask(
                    id=uuid.uuid4(),
                    plan_id=plan.id,
                    project_id=project_id,
                    key=t.key,
                    title=t.title[:300],
                    kind=t.kind,
                    description=t.description,
                    source_ref=t.source_ref,
                    component_ref=t.component_ref,
                    requirement_ids=[str(r) for r in t.requirement_ids],
                    acceptance_criteria=list(t.acceptance_criteria),
                    depends_on=list(t.depends_on),
                    flags=list(t.flags),
                    blocking_questions=list(t.blocking_questions),
                    notes=list(t.notes),
                    related=list(t.related),
                    dependency_sources=dict(t.dependency_sources),
                    order_index=t.order_index,
                    status="todo",
                )
                for t in spec.tasks
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(plan)
        return plan

    def edits(self, project_id: uuid.UUID, plan_id: uuid.UUID) -> Sequence[PlanDependencyEdit]:
        stmt = (
            select(PlanDependencyEdit)
            .where(
                PlanDependencyEdit.project_id == project_id, PlanDependencyEdit.plan_id == plan_id
            )
            .order_by(PlanDependencyEdit.created_at, PlanDependencyEdit.id)
        )
        return self._session.scalars(stmt).all()

    def apply_edit(
        self,
        plan: ImplementationPlan,
        tasks: Sequence[PlanTask],
        edit: PlanDependencyEdit,
    ) -> None:
        """Persist a validated edit and re-sort `order_index` so dependencies come first."""
        try:
            self._session.add(edit)
            ranked = sorted(tasks, key=lambda t: t.order_index)
            new_order = _topological(ranked)
            if [t.key for t in new_order] != [t.key for t in ranked]:
                for t in ranked:  # unique (plan_id, order_index): move out of the way first
                    t.order_index += 1_000_000
                self._session.flush()
                for n, t in enumerate(new_order, start=1):
                    t.order_index = n
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(plan)

    def set_status(self, plan: ImplementationPlan, status: str) -> ImplementationPlan:
        plan.status = status
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(plan)
        return plan
