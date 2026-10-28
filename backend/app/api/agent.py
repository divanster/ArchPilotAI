import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api.ai import require_non_production
from app.api.deps import (
    AgentReadServiceDep,
    AgentServiceDep,
    LLMProviderDep,
    SessionDep,
    make_agent_service,
)
from app.models.agent import AgentRun, AgentStep
from app.services.agent import RunNotFoundError, RunNotRunningError, is_active
from app.services.patch import (
    PatchBusyError,
    PatchGateError,
    TaskNotFoundError,
)
from app.services.plan import PlanNotFoundError
from app.services.project import ProjectNotFoundError

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["agent"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

NotFound = (ProjectNotFoundError, PlanNotFoundError, TaskNotFoundError, RunNotFoundError)


class StartRunIn(BaseModel):
    acknowledge_blocking_questions: bool = False
    max_steps: int | None = Field(default=None, ge=1, le=50)


class StepOut(BaseModel):
    step_no: int
    action: str
    path: str | None
    status: str
    error_code: str | None
    result_summary: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class RunSummary(BaseModel):
    id: uuid.UUID
    task_key: str
    status: str
    stop_reason: str | None
    stop_requested: bool
    max_steps: int
    steps_taken: int
    token_budget: int
    input_tokens: int
    output_tokens: int
    time_limit_seconds: float
    provider: str
    model: str
    prompt_version: str
    summary: str | None
    patch_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int


class RunResponse(RunSummary):
    steps: list[StepOut]


def _duration(run: AgentRun) -> int:
    if run.duration_ms is not None:
        return run.duration_ms
    started = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=UTC)
    return max(int((datetime.now(UTC) - started).total_seconds() * 1000), 0)


def _summary(run: AgentRun) -> RunSummary:
    return RunSummary(
        id=run.id,
        task_key=run.task_key,
        status=run.status,
        stop_reason=run.stop_reason,
        stop_requested=run.stop_requested,
        max_steps=run.max_steps,
        steps_taken=run.steps_taken,
        token_budget=run.token_budget,
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        time_limit_seconds=run.time_limit_seconds,
        provider=run.provider,
        model=run.model,
        prompt_version=run.prompt_version,
        summary=run.summary,
        patch_id=run.patch_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=_duration(run),
    )


def _full(run: AgentRun, steps: list[AgentStep]) -> RunResponse:
    return RunResponse(
        **_summary(run).model_dump(),
        steps=[
            StepOut(
                step_no=s.step_no,
                action=s.action,
                path=s.path,
                status=s.status,
                error_code=s.error_code,
                result_summary=s.result_summary,
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
                latency_ms=s.latency_ms,
            )
            for s in steps
        ],
    )


def _spawn(job: Callable[[], None]) -> None:
    """Run a job in the background. Replaced in tests to run inline."""
    threading.Thread(target=job, name="agent-run", daemon=True).start()


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.post(
    "/plans/{plan_id}/tasks/{task_key}/agent-runs",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_run(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    service: AgentServiceDep,
    session: SessionDep,
    provider: LLMProviderDep,
    body: StartRunIn | None = None,
) -> RunResponse:
    body = body or StartRunIn()
    try:
        run = service.start_run(
            project_id,
            plan_id,
            task_key,
            acknowledge_blocking_questions=body.acknowledge_blocking_questions,
            max_steps=body.max_steps,
        )
    except NotFound as exc:
        raise _not_found(exc) from exc
    except PatchGateError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "message": str(exc)}
        ) from exc
    except PatchBusyError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    run_id = run.id
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)

    def job() -> None:
        session: Session = factory()
        try:
            make_agent_service(session, provider).execute(project_id, run_id)
        finally:
            session.close()

    _spawn(job)
    return _full(service.get_run(project_id, run_id), service.steps(project_id, run_id))


@router.get("/plans/{plan_id}/tasks/{task_key}/agent-runs", response_model=list[RunSummary])
def list_runs(
    project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, service: AgentReadServiceDep
) -> list[RunSummary]:
    try:
        return [_summary(r) for r in service.runs_for_task(project_id, plan_id, task_key)]
    except NotFound as exc:
        raise _not_found(exc) from exc


@router.get("/agent-runs/{run_id}", response_model=RunResponse)
def get_run(project_id: uuid.UUID, run_id: uuid.UUID, service: AgentReadServiceDep) -> RunResponse:
    try:
        run = service.get_run(project_id, run_id)
        if run.status == "running" and not is_active(run.id):
            run = service.mark_interrupted(project_id, run_id)
        return _full(run, service.steps(project_id, run_id))
    except NotFound as exc:
        raise _not_found(exc) from exc


@router.post("/agent-runs/{run_id}/stop", response_model=RunResponse)
def stop_run(project_id: uuid.UUID, run_id: uuid.UUID, service: AgentReadServiceDep) -> RunResponse:
    try:
        run = service.stop(project_id, run_id)
        return _full(run, service.steps(project_id, run_id))
    except NotFound as exc:
        raise _not_found(exc) from exc
    except RunNotRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
