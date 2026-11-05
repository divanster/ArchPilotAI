import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.agent import RunResponse, StartRunIn, launch_run
from app.api.ai import _upstream_error, require_non_production
from app.api.architecture_schemas import ProposalSummary
from app.api.deps import (
    AgentServiceDep,
    ChangeRequestReadServiceDep,
    ChangeRequestServiceDep,
    LLMProviderDep,
    SessionDep,
)
from app.domain.architecture import (
    ArchitectureError,
    ContextTooLargeError,
    NoConfirmedRequirementsError,
)
from app.llm.exceptions import LLMError
from app.models.change_request import ChangeRequest
from app.services.architecture import (
    ProposalNotEditableError,
    ProposalNotFoundError,
    StaleProposalError,
)
from app.services.architecture_generation import ArchitectureBusyError
from app.services.change_request import (
    ChangeRequestInputError,
    ChangeRequestNotFoundError,
    ChangeRequestService,
    ChangeRequestStateError,
    NoBaselineError,
    RemovalConfirmation,
    effective_class,
)
from app.services.plan import (
    EmptyPlanDeltaError,
    GraphNotBuiltError,
    NoApprovedArchitectureError,
    PlanNotFoundError,
)
from app.services.project import ProjectNotFoundError

router = APIRouter(
    prefix="/api/projects/{project_id}/change-requests",
    tags=["change-requests"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

NotFound = (ProjectNotFoundError, ChangeRequestNotFoundError, ProposalNotFoundError)
Conflict = (
    ChangeRequestStateError,
    NoBaselineError,
    ProposalNotEditableError,
    StaleProposalError,
    NoApprovedArchitectureError,
    GraphNotBuiltError,
    PlanNotFoundError,
    EmptyPlanDeltaError,
    NoConfirmedRequirementsError,
    ArchitectureBusyError,
)


class CreateIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1, max_length=100)


class ClassIn(BaseModel):
    impact_class: str
    by: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class DecideIn(BaseModel):
    by: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=1000)


class RemovalIn(BaseModel):
    kind: str
    key: str
    reason: str = Field(min_length=1, max_length=500)


class ApproveArchitectureIn(BaseModel):
    confirmed_removals: list[RemovalIn] = Field(default_factory=list)


class RunIn(StartRunIn):
    task_key: str | None = None


class ChangeRequestSummary(BaseModel):
    id: uuid.UUID
    key: str
    text: str
    author: str
    status: str
    impact_class: str | None
    class_override: str | None
    effective_class: str | None
    drift_flagged: bool
    created_at: datetime


class ChangeRequestDetail(ChangeRequestSummary):
    impact: dict[str, Any] | None
    overridden_by: str | None
    override_reason: str | None
    decided_by: str | None
    decision_reason: str | None
    drift: list[dict[str, Any]]
    proposals: list[dict[str, Any]]
    log: dict[str, Any]


def _summary(cr: ChangeRequest) -> ChangeRequestSummary:
    return ChangeRequestSummary(
        id=cr.id,
        key=cr.key,
        text=cr.text,
        author=cr.author,
        status=cr.status,
        impact_class=cr.impact_class,
        class_override=cr.class_override,
        effective_class=effective_class(cr),
        drift_flagged=bool(cr.drift),
        created_at=cr.created_at,
    )


def _detail(
    service: ChangeRequestService, project_id: uuid.UUID, cr: ChangeRequest
) -> ChangeRequestDetail:
    proposals = [
        {
            **ProposalSummary.model_validate(p).model_dump(mode="json", exclude_none=False),
            "diff": service.diff(project_id, p),
        }
        for p in service.proposals(project_id, cr)
    ]
    return ChangeRequestDetail(
        **_summary(cr).model_dump(),
        impact=cr.impact,
        overridden_by=cr.overridden_by,
        override_reason=cr.override_reason,
        decided_by=cr.decided_by,
        decision_reason=cr.decision_reason,
        drift=list(cr.drift or []),
        proposals=proposals,
        log=service.log(project_id, cr),
    )


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, Conflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, ChangeRequestInputError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, ContextTooLargeError):
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc))
    if isinstance(exc, ArchitectureError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, LLMError):
        return _upstream_error(exc)
    raise exc


HANDLED = (*NotFound, *Conflict, ChangeRequestInputError, ArchitectureError, LLMError)


@router.post("", response_model=ChangeRequestDetail, status_code=status.HTTP_201_CREATED)
def create_change_request(
    project_id: uuid.UUID, body: CreateIn, service: ChangeRequestReadServiceDep
) -> ChangeRequestDetail:
    try:
        cr = service.create(project_id, body.text, body.author)
        return _detail(service, project_id, cr)
    except HANDLED as exc:
        raise _http(exc) from exc


@router.get("", response_model=list[ChangeRequestSummary])
def list_change_requests(
    project_id: uuid.UUID, service: ChangeRequestReadServiceDep
) -> list[ChangeRequestSummary]:
    try:
        return [_summary(cr) for cr in service.list_all(project_id)]
    except HANDLED as exc:
        raise _http(exc) from exc


@router.get("/{change_id}", response_model=ChangeRequestDetail)
def get_change_request(
    project_id: uuid.UUID, change_id: uuid.UUID, service: ChangeRequestReadServiceDep
) -> ChangeRequestDetail:
    try:
        return _detail(service, project_id, service.get(project_id, change_id))
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post("/{change_id}/analyse", response_model=ChangeRequestDetail)
def analyse(
    project_id: uuid.UUID, change_id: uuid.UUID, service: ChangeRequestReadServiceDep
) -> ChangeRequestDetail:
    try:
        return _detail(service, project_id, service.analyse(project_id, change_id))
    except HANDLED as exc:
        raise _http(exc) from exc


@router.put("/{change_id}/class", response_model=ChangeRequestDetail)
def override_class(
    project_id: uuid.UUID,
    change_id: uuid.UUID,
    body: ClassIn,
    service: ChangeRequestReadServiceDep,
) -> ChangeRequestDetail:
    try:
        cr = service.set_class(project_id, change_id, body.impact_class, body.by, body.reason)
        return _detail(service, project_id, cr)
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post("/{change_id}/approve", response_model=ChangeRequestDetail)
def approve(
    project_id: uuid.UUID,
    change_id: uuid.UUID,
    body: DecideIn,
    service: ChangeRequestReadServiceDep,
) -> ChangeRequestDetail:
    try:
        return _detail(
            service, project_id, service.approve(project_id, change_id, body.by, body.reason)
        )
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post("/{change_id}/reject", response_model=ChangeRequestDetail)
def reject(
    project_id: uuid.UUID,
    change_id: uuid.UUID,
    body: DecideIn,
    service: ChangeRequestReadServiceDep,
) -> ChangeRequestDetail:
    try:
        cr = service.reject(project_id, change_id, body.by, body.reason or "")
        return _detail(service, project_id, cr)
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post("/{change_id}/done", response_model=ChangeRequestDetail)
def done(
    project_id: uuid.UUID, change_id: uuid.UUID, service: ChangeRequestReadServiceDep
) -> ChangeRequestDetail:
    try:
        return _detail(service, project_id, service.mark_done(project_id, change_id))
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post(
    "/{change_id}/architecture",
    response_model=ChangeRequestDetail,
    status_code=status.HTTP_201_CREATED,
)
def propose_architecture(
    project_id: uuid.UUID, change_id: uuid.UUID, service: ChangeRequestServiceDep
) -> ChangeRequestDetail:
    try:
        service.propose_architecture(project_id, change_id)
        return _detail(service, project_id, service.get(project_id, change_id))
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post(
    "/{change_id}/propose-architecture-update",
    response_model=ChangeRequestDetail,
    status_code=status.HTTP_201_CREATED,
)
def propose_architecture_update(
    project_id: uuid.UUID, change_id: uuid.UUID, body: DecideIn, service: ChangeRequestServiceDep
) -> ChangeRequestDetail:
    try:
        service.propose_architecture_update(project_id, change_id, body.by)
        return _detail(service, project_id, service.get(project_id, change_id))
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post("/{change_id}/architecture/approve", response_model=ChangeRequestDetail)
def approve_architecture(
    project_id: uuid.UUID,
    change_id: uuid.UUID,
    body: ApproveArchitectureIn,
    service: ChangeRequestReadServiceDep,
) -> ChangeRequestDetail:
    try:
        service.approve_architecture(
            project_id,
            change_id,
            [RemovalConfirmation(r.kind, r.key, r.reason) for r in body.confirmed_removals],
        )
        return _detail(service, project_id, service.get(project_id, change_id))
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post(
    "/{change_id}/plan-delta",
    response_model=ChangeRequestDetail,
    status_code=status.HTTP_201_CREATED,
)
def plan_delta(
    project_id: uuid.UUID, change_id: uuid.UUID, service: ChangeRequestReadServiceDep
) -> ChangeRequestDetail:
    try:
        service.plan_delta(project_id, change_id)
        return _detail(service, project_id, service.get(project_id, change_id))
    except HANDLED as exc:
        raise _http(exc) from exc


@router.post("/{change_id}/run", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def run_fast_path(
    project_id: uuid.UUID,
    change_id: uuid.UUID,
    agent: AgentServiceDep,
    changes: ChangeRequestReadServiceDep,
    session: SessionDep,
    provider: LLMProviderDep,
    body: RunIn | None = None,
) -> RunResponse:
    """The fast path: an agent run on an existing approved task, with the request as the task
    text. The same gates, hooks, tiers, approvals and budgets as any task run apply."""
    body = body or RunIn()
    try:
        cr = changes.get(project_id, change_id)
        plan, task = changes.anchor_task(project_id, cr, body.task_key)
        fresh = changes.take_baseline(project_id, cr)
        try:
            response = launch_run(
                project_id,
                plan.id,
                task.key,
                agent,
                session,
                provider,
                body,
                change_request_id=cr.id,
            )
        except HTTPException:
            if fresh:
                changes.clear_baseline(cr)
            raise
        changes.mark_started(cr)
        return response
    except HANDLED as exc:
        raise _http(exc) from exc
