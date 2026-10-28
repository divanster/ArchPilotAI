import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.ai import require_non_production
from app.api.deps import PatchRepositoryDep, PlanServiceDep
from app.domain.patch import task_status
from app.domain.plan import PlanCycleError
from app.models.plan import PlanTask
from app.services.plan import (
    GraphNotBuiltError,
    NoApprovedArchitectureError,
    PlanDetail,
    PlanEditError,
    PlanNotDraftError,
    PlanNotFoundError,
)
from app.services.project import ProjectNotFoundError

router = APIRouter(
    prefix="/api/projects/{project_id}/plans",
    tags=["plans"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

NotFound = (ProjectNotFoundError, PlanNotFoundError)


class DependencyOut(BaseModel):
    key: str
    source: str  # "diagram" | "nfr rule" | "manual"
    reason: str | None = None
    author: str | None = None
    created_at: datetime | None = None


class PlanTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    title: str
    kind: str
    description: str
    source_ref: str
    component_ref: str | None
    requirement_ids: list[uuid.UUID]
    acceptance_criteria: list[str]
    depends_on: list[str]
    dependencies: list[DependencyOut] = Field(default_factory=list)
    flags: list[str]
    blocking_questions: list[str]
    notes: list[str]
    related: list[str]
    order_index: int
    status: str


class ManualEditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: str
    task_key: str
    depends_on_key: str
    reason: str
    author: str
    created_at: datetime


class DependencyEditIn(BaseModel):
    task_id: uuid.UUID
    depends_on_task_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=100)


class PlanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    proposal_id: uuid.UUID
    version: int
    status: str
    generated_at: datetime
    source_graph_built_at: datetime | None
    task_count: int = 0


class PlanResponse(PlanSummary):
    unattached_questions: list[str] = Field(default_factory=list)
    not_for_client: list[str] = Field(default_factory=list)
    dependency_count: int = 0
    unmapped_node_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    manual_edits: list[ManualEditOut] = Field(default_factory=list)
    previous_version: int | None = None
    previous_manual_edits: list[ManualEditOut] = Field(default_factory=list)
    tasks: list[PlanTaskOut]


def _task_out(detail: PlanDetail, task: PlanTask, derived: dict[uuid.UUID, str]) -> PlanTaskOut:
    out = PlanTaskOut.model_validate(task)
    # todo / proposed / accepted is derived from the task's patches.
    out.status = derived.get(task.id, "todo")
    for key in task.depends_on:
        edit = detail.manual_reason(task, key)
        out.dependencies.append(
            DependencyOut(
                key=key,
                source=task.source_of(key),
                reason=edit.reason if edit else None,
                author=edit.author if edit else None,
                created_at=edit.created_at if edit else None,
            )
        )
    return out


def _full(detail: PlanDetail, derived: dict[uuid.UUID, str] | None = None) -> PlanResponse:
    statuses = derived or {}
    return PlanResponse(
        **PlanSummary.model_validate(detail.plan).model_dump(exclude={"task_count"}),
        task_count=len(detail.tasks),
        unattached_questions=list(detail.plan.unattached_questions),
        not_for_client=list(detail.plan.not_for_client),
        dependency_count=detail.plan.dependency_count,
        unmapped_node_count=detail.plan.unmapped_node_count,
        warnings=list(detail.plan.warnings),
        manual_edits=[ManualEditOut.model_validate(e) for e in detail.edits],
        previous_version=detail.previous_version,
        previous_manual_edits=[ManualEditOut.model_validate(e) for e in detail.previous_edits],
        tasks=[_task_out(detail, t, statuses) for t in detail.tasks],
    )


@router.post("", response_model=PlanResponse, status_code=status.HTTP_201_CREATED)
def build_plan(project_id: uuid.UUID, service: PlanServiceDep) -> PlanResponse:
    try:
        return _full(service.build(project_id))
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (NoApprovedArchitectureError, GraphNotBuiltError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PlanCycleError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("", response_model=list[PlanSummary])
def list_plans(project_id: uuid.UUID, service: PlanServiceDep) -> list[PlanSummary]:
    try:
        plans, counts = service.list(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    out = []
    for plan in plans:
        summary = PlanSummary.model_validate(plan)
        summary.task_count = counts[plan.id]
        out.append(summary)
    return out


@router.get("/{plan_id}", response_model=PlanResponse)
def get_plan(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    service: PlanServiceDep,
    patches: PatchRepositoryDep,
) -> PlanResponse:
    try:
        detail = service.get(project_id, plan_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    derived = {
        task_id: task_status(found)
        for task_id, found in patches.statuses_by_task(project_id, plan_id).items()
    }
    return _full(detail, derived)


def _decide(
    project_id: uuid.UUID, plan_id: uuid.UUID, service: PlanServiceDep, new_status: str
) -> PlanResponse:
    try:
        return _full(service.decide(project_id, plan_id, new_status))
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PlanNotDraftError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/{plan_id}/approve", response_model=PlanResponse)
def approve_plan(
    project_id: uuid.UUID, plan_id: uuid.UUID, service: PlanServiceDep
) -> PlanResponse:
    return _decide(project_id, plan_id, service, "approved")


@router.post("/{plan_id}/reject", response_model=PlanResponse)
def reject_plan(project_id: uuid.UUID, plan_id: uuid.UUID, service: PlanServiceDep) -> PlanResponse:
    return _decide(project_id, plan_id, service, "rejected")


def _edit(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    body: DependencyEditIn,
    service: PlanServiceDep,
    action: str,
) -> PlanResponse:
    method = service.add_dependency if action == "add" else service.remove_dependency
    try:
        return _full(
            method(
                project_id, plan_id, body.task_id, body.depends_on_task_id, body.reason, body.author
            )
        )
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PlanNotDraftError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PlanEditError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/{plan_id}/dependencies", response_model=PlanResponse)
def add_dependency(
    project_id: uuid.UUID, plan_id: uuid.UUID, body: DependencyEditIn, service: PlanServiceDep
) -> PlanResponse:
    return _edit(project_id, plan_id, body, service, "add")


@router.post("/{plan_id}/dependencies/remove", response_model=PlanResponse)
def remove_dependency(
    project_id: uuid.UUID, plan_id: uuid.UUID, body: DependencyEditIn, service: PlanServiceDep
) -> PlanResponse:
    return _edit(project_id, plan_id, body, service, "remove")
