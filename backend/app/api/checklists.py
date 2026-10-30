"""Task checklists (Phase 13D): propose, revise, approve and run a task one step at a time."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api import agent as agent_api
from app.api.ai import require_non_production
from app.api.deps import (
    LLMProviderDep,
    SessionDep,
    agent_deployments,
    make_agent_service,
    provider_for_deployment,
)
from app.domain.checklist import ChecklistInvalidError, step_warnings
from app.llm.contracts import LLMProvider
from app.models.agent import AgentRun
from app.models.checklist import TaskChecklist
from app.repositories.agent import AgentRepository
from app.repositories.checklist import ChecklistRepository
from app.repositories.patch import PatchRepository
from app.services.agent import CheckpointStaleError
from app.services.checklist import (
    AgentRevisionError,
    ChecklistNotFoundError,
    ChecklistReadOnlyError,
    ChecklistService,
    ChecklistStateError,
    ChecklistView,
    DeciderError,
    StepState,
)
from app.services.patch import PatchBusyError, PatchGateError

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["checklists"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

BASE = "/plans/{plan_id}/tasks/{task_key}/checklist"


def get_checklist_service(session: SessionDep, provider: LLMProviderDep) -> ChecklistService:
    return ChecklistService(
        provider,
        make_agent_service(session, provider),
        ChecklistRepository(session),
        AgentRepository(session),
        PatchRepository(session),
    )


ChecklistServiceDep = Annotated[ChecklistService, Depends(get_checklist_service)]


# ---- schemas -----------------------------------------------------------------------------------


class ExpectedFileIO(BaseModel):
    path: str = Field(max_length=300)
    # task | dependency | repository: an existing file; new: a file the step creates.
    source: str = Field(default="new", max_length=20)


class CriterionReasonIO(BaseModel):
    criterion: str = Field(max_length=2000)
    reason: str = Field(max_length=1000)


class RelatedItemOut(BaseModel):
    node_id: str
    kind: str
    label: str


class StepIn(BaseModel):
    key: str = Field(max_length=20)
    title: str = Field(max_length=400)
    goal: str = Field(default="", max_length=2000)
    acceptance_criteria: list[str] = Field(default=[], max_length=100)
    expected_files: list[ExpectedFileIO] = Field(default=[], max_length=50)
    depends_on: list[str] = Field(default=[], max_length=50)
    grouping_reason: str = Field(default="", max_length=1000)
    discovery_note: str = Field(default="", max_length=600)
    shared_criteria: list[CriterionReasonIO] = Field(default=[], max_length=100)


class ReviseIn(BaseModel):
    author: str = Field(max_length=100)
    reason: str = Field(max_length=1000)
    steps: list[StepIn] = Field(max_length=100)
    manual_verification: list[CriterionReasonIO] = Field(default=[], max_length=100)


class AgentRevisionIn(BaseModel):
    instruction: str = Field(default="", max_length=2000)


class DecisionIn(BaseModel):
    by: str = Field(max_length=100)
    reason: str = Field(max_length=1000)


class RunStepIn(BaseModel):
    acknowledge_blocking_questions: bool = False
    restart: bool = False  # start the step over instead of resuming its checkpoint
    max_steps: int | None = Field(default=None, ge=1, le=50)
    deployment: str | None = Field(default=None, max_length=100)


class StepRunOut(BaseModel):
    id: uuid.UUID
    status: str
    stop_reason: str | None
    steps_taken: int
    resumed_from_run_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None


class StepPatchOut(BaseModel):
    id: uuid.UUID
    version: int
    status: str


class StepChecksOut(BaseModel):
    tests: str | None = None  # status of the last run_tests step
    finding_total: int | None = None  # actionable findings of the last static check


class StepOut(BaseModel):
    key: str
    title: str
    goal: str
    acceptance_criteria: list[str]
    expected_files: list[ExpectedFileIO]
    depends_on: list[str]
    related_items: list[RelatedItemOut]
    grouping_reason: str
    discovery_note: str
    shared_criteria: list[CriterionReasonIO]
    criteria_count: int
    warnings: list[str]  # step quality: the step is valid but may be too large or too vague
    status: str  # todo | running | proposed | accepted | rejected | failed | stopped
    resumable: bool
    run: StepRunOut | None
    patch: StepPatchOut | None
    checks: StepChecksOut


class ChecklistVersionOut(BaseModel):
    id: uuid.UUID
    version: int
    status: str
    proposed_by: str
    proposal_reason: str | None
    decided_by: str | None
    decision_reason: str | None
    created_at: datetime
    decided_at: datetime | None


class ChecklistOut(ChecklistVersionOut):
    task_key: str
    read_only: bool
    warnings: list[str]
    manual_verification: list[CriterionReasonIO]
    steps: list[StepOut]


class ChecklistViewOut(BaseModel):
    task_key: str
    checklist: ChecklistOut | None
    next_step: str | None
    history: list[ChecklistVersionOut]


# ---- mapping -----------------------------------------------------------------------------------


def _version(c: TaskChecklist) -> ChecklistVersionOut:
    return ChecklistVersionOut(
        id=c.id,
        version=c.version,
        status=c.status,
        proposed_by=c.proposed_by,
        proposal_reason=c.proposal_reason,
        decided_by=c.decided_by,
        decision_reason=c.decision_reason,
        created_at=c.created_at,
        decided_at=c.decided_at,
    )


def _checks(service: ChecklistService, run: AgentRun | None) -> StepChecksOut:
    if run is None:
        return StepChecksOut()
    tests: str | None = None
    total: int | None = None
    for step in service.run_steps(run):
        found = agent_api._tests(step)
        if found is not None:
            tests = found.status
        findings = agent_api._findings(step)
        if "finding_total" in findings:
            total = int(findings["finding_total"])
    return StepChecksOut(tests=tests, finding_total=total)


def _step(service: ChecklistService, state: StepState) -> StepOut:
    run, patch = state.run, state.patch
    return StepOut(
        **{k: v for k, v in state.step.as_json().items()},
        criteria_count=len(state.step.acceptance_criteria),
        warnings=step_warnings(state.step),
        status=state.status,
        resumable=state.resumable,
        run=StepRunOut(
            id=run.id,
            status=run.status,
            stop_reason=run.stop_reason,
            steps_taken=run.steps_taken,
            resumed_from_run_id=run.resumed_from_run_id,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
        if run is not None
        else None,
        patch=StepPatchOut(id=patch.id, version=patch.version, status=patch.status)
        if patch is not None
        else None,
        checks=_checks(service, run),
    )


def _checklist(
    service: ChecklistService, c: TaskChecklist, states: Sequence[StepState]
) -> ChecklistOut:
    return ChecklistOut(
        **_version(c).model_dump(),
        task_key=c.task_key,
        read_only=c.status != "proposed",
        warnings=list(c.warnings),
        manual_verification=[CriterionReasonIO(**m) for m in c.manual_verification or []],
        steps=[_step(service, s) for s in states],
    )


def _view(service: ChecklistService, view: ChecklistView) -> ChecklistViewOut:
    return ChecklistViewOut(
        task_key=view.task_key,
        checklist=_checklist(service, view.checklist, view.steps) if view.checklist else None,
        next_step=view.next_step,
        history=[_version(c) for c in view.history],
    )


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (*agent_api.NotFound, ChecklistNotFoundError)):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ChecklistInvalidError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {
                "code": "checklist_invalid",
                "message": str(exc),
                "problems": [p.as_json() for p in exc.problems],
            },
        )
    if isinstance(exc, DeciderError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": "invalid_decider", "message": str(exc)}
        )
    if isinstance(exc, PatchGateError | ChecklistStateError):
        return HTTPException(status.HTTP_409_CONFLICT, {"code": exc.code, "message": str(exc)})
    if isinstance(exc, ChecklistReadOnlyError):
        return HTTPException(
            status.HTTP_409_CONFLICT, {"code": "checklist_read_only", "message": str(exc)}
        )
    if isinstance(exc, CheckpointStaleError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "checkpoint_stale", "message": str(exc), "paths": exc.paths},
        )
    if isinstance(exc, PatchBusyError):
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc))
    if isinstance(exc, AgentRevisionError):
        return HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    raise exc


_HANDLED = (
    *agent_api.NotFound,
    ChecklistNotFoundError,
    ChecklistInvalidError,
    DeciderError,
    PatchGateError,
    ChecklistStateError,
    ChecklistReadOnlyError,
    CheckpointStaleError,
    PatchBusyError,
    AgentRevisionError,
)


def _current(
    service: ChecklistService, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
) -> ChecklistViewOut:
    return _view(service, service.view(project_id, plan_id, task_key))


# ---- routes ------------------------------------------------------------------------------------


@router.get(BASE, response_model=ChecklistViewOut)
def get_checklist(
    project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, service: ChecklistServiceDep
) -> ChecklistViewOut:
    try:
        return _current(service, project_id, plan_id, task_key)
    except _HANDLED as exc:
        raise _error(exc) from exc


@router.post(BASE, response_model=ChecklistViewOut, status_code=status.HTTP_201_CREATED)
def build_checklist(
    project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, service: ChecklistServiceDep
) -> ChecklistViewOut:
    try:
        service.build(project_id, plan_id, task_key)
        return _current(service, project_id, plan_id, task_key)
    except _HANDLED as exc:
        raise _error(exc) from exc


@router.put(BASE, response_model=ChecklistViewOut)
def revise_checklist(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    body: ReviseIn,
    service: ChecklistServiceDep,
) -> ChecklistViewOut:
    try:
        service.revise(
            project_id,
            plan_id,
            task_key,
            body.author,
            body.reason,
            [s.model_dump() for s in body.steps],
            [m.model_dump() for m in body.manual_verification],
        )
        return _current(service, project_id, plan_id, task_key)
    except _HANDLED as exc:
        raise _error(exc) from exc


@router.post(f"{BASE}/agent-revision", response_model=ChecklistViewOut)
def agent_revision(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    service: ChecklistServiceDep,
    body: AgentRevisionIn | None = None,
) -> ChecklistViewOut:
    try:
        service.agent_revise(project_id, plan_id, task_key, (body or AgentRevisionIn()).instruction)
        return _current(service, project_id, plan_id, task_key)
    except _HANDLED as exc:
        raise _error(exc) from exc


@router.post(f"{BASE}/approve", response_model=ChecklistViewOut)
def approve_checklist(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    body: DecisionIn,
    service: ChecklistServiceDep,
) -> ChecklistViewOut:
    try:
        service.approve(project_id, plan_id, task_key, body.by, body.reason)
        return _current(service, project_id, plan_id, task_key)
    except _HANDLED as exc:
        raise _error(exc) from exc


@router.post(f"{BASE}/reject", response_model=ChecklistViewOut)
def reject_checklist(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    body: DecisionIn,
    service: ChecklistServiceDep,
) -> ChecklistViewOut:
    try:
        service.reject(project_id, plan_id, task_key, body.by, body.reason)
        return _current(service, project_id, plan_id, task_key)
    except _HANDLED as exc:
        raise _error(exc) from exc


def _check_deployment(deployment: str | None) -> None:
    default, verified = agent_deployments()
    if deployment and deployment != default and deployment not in verified:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The deployment is not marked as verified in the settings.",
        )


def _launch(
    service: ChecklistService,
    session: Session,
    provider: LLMProvider,
    project_id: uuid.UUID,
    run: AgentRun,
    deployment: str | None,
) -> agent_api.RunResponse:
    run_id = run.id
    run_provider = provider_for_deployment(provider, deployment)
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)

    def job() -> None:
        worker: Session = factory()
        try:
            make_agent_service(worker, run_provider).execute(project_id, run_id)
        finally:
            worker.close()

    agent_api._spawn(job)
    agent = service.agent
    return agent_api._full(
        agent.get_run(project_id, run_id),
        agent.steps(project_id, run_id),
        agent.jobs(project_id, run_id),
        agent.hook_states(project_id),
    )


@router.post(
    f"{BASE}/run-next", response_model=agent_api.RunResponse, status_code=status.HTTP_202_ACCEPTED
)
def run_next_step(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    service: ChecklistServiceDep,
    session: SessionDep,
    provider: LLMProviderDep,
    body: RunStepIn | None = None,
) -> agent_api.RunResponse:
    body = body or RunStepIn()
    _check_deployment(body.deployment)
    try:
        run = service.run_next(
            project_id,
            plan_id,
            task_key,
            acknowledge_blocking_questions=body.acknowledge_blocking_questions,
            restart=body.restart,
            max_steps=body.max_steps,
            deployment=body.deployment,
        )
    except _HANDLED as exc:
        raise _error(exc) from exc
    return _launch(service, session, provider, project_id, run, body.deployment)


@router.post(
    f"{BASE}/steps/{{step_key}}/resume",
    response_model=agent_api.RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume_step(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    step_key: str,
    service: ChecklistServiceDep,
    session: SessionDep,
    provider: LLMProviderDep,
    body: RunStepIn | None = None,
) -> agent_api.RunResponse:
    body = body or RunStepIn()
    _check_deployment(body.deployment)
    try:
        run = service.resume(
            project_id,
            plan_id,
            task_key,
            step_key,
            acknowledge_blocking_questions=body.acknowledge_blocking_questions,
            max_steps=body.max_steps,
            deployment=body.deployment,
        )
    except _HANDLED as exc:
        raise _error(exc) from exc
    return _launch(service, session, provider, project_id, run, body.deployment)


__all__ = ["router", "get_checklist_service"]
