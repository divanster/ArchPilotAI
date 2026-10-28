import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.ai import require_non_production
from app.api.deps import PlanServiceDep
from app.domain.plan import PlanCycleError
from app.services.plan import (
    GraphNotBuiltError,
    NoApprovedArchitectureError,
    PlanDetail,
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
    flags: list[str]
    blocking_questions: list[str]
    order_index: int
    status: str


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
    tasks: list[PlanTaskOut]


def _full(detail: PlanDetail) -> PlanResponse:
    return PlanResponse(
        **PlanSummary.model_validate(detail.plan).model_dump(exclude={"task_count"}),
        task_count=len(detail.tasks),
        unattached_questions=list(detail.plan.unattached_questions),
        tasks=[PlanTaskOut.model_validate(t) for t in detail.tasks],
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
def get_plan(project_id: uuid.UUID, plan_id: uuid.UUID, service: PlanServiceDep) -> PlanResponse:
    try:
        return _full(service.get(project_id, plan_id))
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


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
