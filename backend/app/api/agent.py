import json
import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from app.api.ai import require_non_production
from app.api.deps import (
    AgentReadServiceDep,
    AgentServiceDep,
    LLMProviderDep,
    SessionDep,
    agent_deployments,
    make_agent_service,
    make_static_checker,
    provider_for_deployment,
)
from app.core.config import get_settings
from app.models.agent import AgentJob, AgentRun, AgentStep
from app.models.project import Project
from app.services.agent import (
    HookNotFoundError,
    HookRequiredError,
    RunNotFoundError,
    RunNotRunningError,
    is_active,
)
from app.services.ci_execution import (
    ARTIFACT_NAME,
    CIApprovalError,
    ci_state,
    decide,
    workflow_template,
)
from app.services.patch import (
    PatchBusyError,
    PatchGateError,
    TaskNotFoundError,
)
from app.services.plan import PlanNotFoundError
from app.services.project import ProjectNotFoundError
from app.tools import get_registry

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["agent"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

checks_router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    dependencies=[Depends(require_non_production)],
)


class ToolStatus(BaseModel):
    available: bool
    version: str | None


class ChecksStatus(BaseModel):
    ok: bool  # ruff and mypy are both available: runs can start
    tools: dict[str, ToolStatus]


@checks_router.get("/checks/status", response_model=ChecksStatus)
def checks_status() -> ChecksStatus:
    found = make_static_checker().status()
    required = ("ruff", "mypy")
    return ChecksStatus(
        ok=all(found[name]["available"] for name in required),
        tools={name: ToolStatus(**info) for name, info in found.items()},
    )


NotFound = (ProjectNotFoundError, PlanNotFoundError, TaskNotFoundError, RunNotFoundError)


class StartRunIn(BaseModel):
    acknowledge_blocking_questions: bool = False
    max_steps: int | None = Field(default=None, ge=1, le=50)
    deployment: str | None = Field(default=None, max_length=100)  # verified ones only


class DeploymentsOut(BaseModel):
    default: str
    verified: list[str]


@checks_router.get("/deployments", response_model=DeploymentsOut)
def deployments() -> DeploymentsOut:
    default, verified = agent_deployments()
    return DeploymentsOut(default=default, verified=verified)


class ModelCallOut(BaseModel):
    """What one model call of a step used, as the provider reported it."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    latency_ms: int = 0


class RunUsage(BaseModel):
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0
    finish_reasons: dict[str, int] = {}


class FindingOut(BaseModel):
    tool: str
    file: str
    line: int
    code: str
    message: str
    scope: str | None = None  # "in changed files" | "elsewhere"


class TestsOut(BaseModel):
    """A run_tests step as shown in the run panel (read back from its stored result)."""

    __test__ = False

    command_id: str
    status: str
    structured: bool = False
    new: list[dict[str, Any]] = []
    new_in_changed: int = 0
    new_elsewhere: int = 0
    pre_existing: int = 0
    fixed: int = 0
    flaky: list[str] = []
    baseline: bool = False
    note: str | None = None
    ci: dict[str, Any] | None = None  # CI provider: status, conclusion, workflow_url, branch


class CIJobOut(BaseModel):
    """What a CI job did, from its stored summary. Never holds a token."""

    provider: str = "github_actions"
    status: str | None = None
    branch: str | None = None
    commit: str | None = None
    workflow_run_id: int | None = None
    workflow_url: str | None = None
    conclusion: str | None = None
    report: bool | None = None
    approved_by: str | None = None
    kept: bool | None = None
    branch_deleted: bool | None = None
    error: str | None = None


class CILiveOut(BaseModel):
    """The live CI state of a running run (approval, phase, workflow link)."""

    provider: str
    approval: str  # none | pending | approved | denied
    approved_by: str | None
    keep_branches: bool
    phase: str
    command_id: str | None
    branch: str | None
    workflow_run_id: int | None
    workflow_url: str | None
    conclusion: str | None
    message: str | None


class CIApprovalIn(BaseModel):
    approve: bool
    approved_by: str = Field(min_length=1, max_length=100)
    keep_branches: bool = False


class WorkflowTemplateOut(BaseModel):
    path: str
    workflow_file: str
    artifact: str
    content: str


class JobOut(BaseModel):
    id: str
    command_id: str
    variant: str
    status: str
    timeout_seconds: float
    exit_code: int | None
    duration_ms: int
    truncated: bool
    select_test: str | None
    flaky: bool | None = None  # set on a re-run job
    ci: CIJobOut | None = None  # set on a CI (GitHub Actions) job


class HookDecisionOut(BaseModel):
    hook: str
    point: str
    result: str  # allow | block | annotate
    reason: str


class HookOut(BaseModel):
    name: str
    point: str
    tools: list[str]
    order: int
    required: bool  # cannot be disabled
    description: str
    enabled: bool


class HookEnabledIn(BaseModel):
    enabled: bool


class StepOut(BaseModel):
    step_no: int
    action: str
    tool: str | None = None  # the registry tool the step called (none for an invalid reply)
    tier: str | None = None  # read | draft | approval
    path: str | None
    status: str
    error_code: str | None
    result_summary: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    findings: list[FindingOut] = []
    finding_total: int | None = None  # actionable findings of a run_static_checks step
    findings_not_shown: int = 0  # environment findings counted but not shown
    environment_reasons: dict[str, int] = {}  # why they are environment noise -> count
    scope_counts: dict[str, int] = {}  # findings "in changed files" / "elsewhere"
    tests: TestsOut | None = None
    hooks: list[HookDecisionOut] = []  # what each hook decided for this step
    call: ModelCallOut | None = None  # the model call that chose this step


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
    deployment: str | None = None  # the chat deployment chosen for the run
    prompt_version: str
    summary: str | None
    patch_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int


class RunResponse(RunSummary):
    steps: list[StepOut]
    jobs: list[JobOut] = []
    hooks: list[HookOut] = []  # the hooks active when the run started
    usage: RunUsage = RunUsage()  # totals of the model calls of the run
    ci: CILiveOut | None = None  # live CI state while the run is active


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
        deployment=run.deployment,
        prompt_version=run.prompt_version,
        summary=run.summary,
        patch_id=run.patch_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=_duration(run),
    )


def _findings(step: AgentStep) -> dict[str, Any]:
    """Findings of a static-check step, read back from its stored (bounded) result."""
    if step.action != "run_static_checks" or step.status != "ok":
        return {}
    try:
        data = json.loads(step.result)
        found = [
            FindingOut(
                **{k: f[k] for k in FindingOut.model_fields if k != "scope"}, scope=f.get("scope")
            )
            for f in data["findings"]
        ]
        return {
            "findings": found,
            "finding_total": int(data["total"]),
            "scope_counts": {str(k): int(v) for k, v in (data.get("scope") or {}).items()},
            "findings_not_shown": int((data.get("not_shown") or {}).get("environment", 0)),
            "environment_reasons": {
                str(k): int(v)
                for k, v in ((data.get("not_shown") or {}).get("reasons") or {}).items()
            },
        }
    except (ValueError, KeyError, TypeError):
        return {}


def _tests(step: AgentStep) -> TestsOut | None:
    if step.action != "run_tests" or step.status != "ok":
        return None
    try:
        data = json.loads(step.result)
        fields = {k: data[k] for k in TestsOut.model_fields if k in data}
        return TestsOut(**fields)
    except (ValueError, TypeError):
        return None


def _ci_job(summary: dict[str, Any] | None) -> CIJobOut | None:
    found = (summary or {}).get("ci")
    if not isinstance(found, dict):
        return None
    try:
        return CIJobOut(**{k: found[k] for k in CIJobOut.model_fields if k in found})
    except (ValueError, TypeError):
        return None


def _jobs(jobs: Sequence[AgentJob]) -> list[JobOut]:
    return [
        JobOut(
            id=str((j.summary or {}).get("job", j.id)),
            command_id=j.command_id,
            variant=j.variant,
            status=j.status,
            timeout_seconds=j.timeout_seconds,
            exit_code=j.exit_code,
            duration_ms=j.duration_ms,
            truncated=j.truncated,
            select_test=j.select_test,
            flaky=(j.summary or {}).get("flaky"),
            ci=_ci_job(j.summary),
        )
        for j in jobs
    ]


def _tier(action: str) -> str | None:
    found = get_registry().find("agent", action)
    return found.tier if found else None


def _hook_log(step: AgentStep) -> list[HookDecisionOut]:
    found = (step.args or {}).get("hooks")
    if not isinstance(found, list):
        return []
    return [
        HookDecisionOut(**{k: str(h.get(k, "")) for k in HookDecisionOut.model_fields})
        for h in found
    ]


def _call(step: AgentStep) -> ModelCallOut | None:
    found = (step.args or {}).get("call")
    if not isinstance(found, dict):
        return None
    return ModelCallOut(**{k: found.get(k) for k in ModelCallOut.model_fields if k in found})


def _usage(calls: Sequence[ModelCallOut]) -> RunUsage:
    reasons: dict[str, int] = {}
    for c in calls:
        if c.finish_reason:
            reasons[c.finish_reason] = reasons.get(c.finish_reason, 0) + 1
    return RunUsage(
        calls=len(calls),
        prompt_tokens=sum(c.prompt_tokens or 0 for c in calls),
        completion_tokens=sum(c.completion_tokens or 0 for c in calls),
        reasoning_tokens=sum(c.reasoning_tokens or 0 for c in calls),
        latency_ms=sum(c.latency_ms for c in calls),
        finish_reasons=reasons,
    )


def _full(
    run: AgentRun,
    steps: list[AgentStep],
    jobs: Sequence[AgentJob] = (),
    hooks: Sequence[dict[str, Any]] = (),
) -> RunResponse:
    calls = [c for s in steps if (c := _call(s)) is not None]
    live = ci_state(run.project_id, run.id) if run.status == "running" else None
    return RunResponse(
        **_summary(run).model_dump(),
        jobs=_jobs(jobs),
        usage=_usage(calls),
        ci=CILiveOut(**live) if live else None,
        hooks=[HookOut(**h) for h in (run.hooks if run.hooks is not None else hooks)],
        steps=[
            StepOut(
                step_no=s.step_no,
                action=s.action,
                tool=s.action if _tier(s.action) else None,
                tier=_tier(s.action),
                path=s.path,
                status=s.status,
                error_code=s.error_code,
                result_summary=s.result_summary,
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
                latency_ms=s.latency_ms,
                **_findings(s),
                tests=_tests(s),
                hooks=_hook_log(s),
                call=_call(s),
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
    default, verified = agent_deployments()
    if body.deployment and body.deployment != default and body.deployment not in verified:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "The deployment is not marked as verified in the settings.",
        )
    try:
        run = service.start_run(
            project_id,
            plan_id,
            task_key,
            acknowledge_blocking_questions=body.acknowledge_blocking_questions,
            max_steps=body.max_steps,
            deployment=body.deployment,
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
    run_provider = provider_for_deployment(provider, body.deployment)
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)

    def job() -> None:
        session: Session = factory()
        try:
            make_agent_service(session, run_provider).execute(project_id, run_id)
        finally:
            session.close()

    _spawn(job)
    return _full(
        service.get_run(project_id, run_id),
        service.steps(project_id, run_id),
        service.jobs(project_id, run_id),
        service.hook_states(project_id),
    )


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
        return _full(
            run,
            service.steps(project_id, run_id),
            service.jobs(project_id, run_id),
            service.hook_states(project_id),
        )
    except NotFound as exc:
        raise _not_found(exc) from exc


@router.post("/agent-runs/{run_id}/stop", response_model=RunResponse)
def stop_run(project_id: uuid.UUID, run_id: uuid.UUID, service: AgentReadServiceDep) -> RunResponse:
    try:
        run = service.stop(project_id, run_id)
        return _full(
            run,
            service.steps(project_id, run_id),
            service.jobs(project_id, run_id),
            service.hook_states(project_id),
        )
    except NotFound as exc:
        raise _not_found(exc) from exc
    except RunNotRunningError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/agent-runs/{run_id}/ci/approval", response_model=CILiveOut)
def approve_ci_push(
    project_id: uuid.UUID, run_id: uuid.UUID, body: CIApprovalIn, service: AgentReadServiceDep
) -> CILiveOut:
    """The user's answer to a run's CI push (once per run). Only a person calls this: it is not
    a tool, and the agent has no way to reach it."""
    try:
        run = service.get_run(project_id, run_id)
    except NotFound as exc:
        raise _not_found(exc) from exc
    if run.status != "running":
        raise HTTPException(status.HTTP_409_CONFLICT, "The run has already ended")
    try:
        state = decide(
            project_id,
            run_id,
            approve=body.approve,
            approved_by=body.approved_by.strip() or "user",
            keep_branches=body.keep_branches,
        )
    except CIApprovalError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return CILiveOut(**state)


@checks_router.get("/ci/workflow-template", response_model=WorkflowTemplateOut)
def ci_workflow_template() -> WorkflowTemplateOut:
    """The workflow the user may add to the target repository; ArchPilotAI never pushes it."""
    workflow = get_settings().agent_ci_workflow
    return WorkflowTemplateOut(
        path=f".github/workflows/{workflow}",
        workflow_file=workflow,
        artifact=ARTIFACT_NAME,
        content=workflow_template(),
    )


@router.get("/agent-hooks", response_model=list[HookOut])
def list_hooks(
    project_id: uuid.UUID, service: AgentReadServiceDep, session: SessionDep
) -> list[HookOut]:
    if session.get(Project, project_id) is None:
        raise _not_found(ProjectNotFoundError(project_id))
    return [HookOut(**h) for h in service.hook_states(project_id)]


@router.put("/agent-hooks/{name}", response_model=list[HookOut])
def set_hook(
    project_id: uuid.UUID,
    name: str,
    body: HookEnabledIn,
    service: AgentReadServiceDep,
    session: SessionDep,
) -> list[HookOut]:
    if session.get(Project, project_id) is None:
        raise _not_found(ProjectNotFoundError(project_id))
    try:
        service.set_hook_enabled(project_id, name, body.enabled)
    except HookNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except HookRequiredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return [HookOut(**h) for h in service.hook_states(project_id)]
