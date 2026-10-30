"""Run the agent on seeded projects and collect per-run metrics.

Nothing here talks to a real model by itself: the caller passes a provider factory. The CLI
(`app.eval.run`) only builds a real provider when `--live` is given.
"""

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.domain.execution import UnavailableExecutionProvider
from app.domain.patch import PatchLimits, removed_from_accepted
from app.eval.seed import SeededProject, patch_service, seed_project
from app.llm.contracts import LLMProvider
from app.mcp.tools import ToolContext
from app.repositories.agent import AgentRepository
from app.repositories.patch import PatchRepository
from app.repositories.plan import PlanRepository
from app.services.agent import AgentConfig, AgentService
from app.services.execution import TestRunner
from app.services.static_checks import CheckReport, StaticChecker

ProviderFactory = Callable[[str], LLMProvider]


class Checker(Protocol):
    def run(self, files: Mapping[str, str]) -> CheckReport: ...


@dataclass
class RunMetrics:
    deployment: str
    repeat: int
    task_key: str
    run_id: str
    provider: str
    model: str
    status: str
    stop_reason: str
    steps: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None  # None when the provider never reported it
    total_tokens: int
    latency_ms: int  # wall time of the run
    model_latency_ms: int  # sum of the model calls
    coverage_covered: int
    coverage_missing: int
    coverage_unchecked: int
    removals_from_accepted: int  # definitions removed from modified accepted files
    deleted_accepted_files: int
    static_findings: int
    static_unavailable: str  # required checkers that could not run, ";"-joined
    patch_id: str
    error: str  # exception type when the run could not start


CSV_FIELDS = [f.name for f in fields(RunMetrics)]


class SnapshotAgentRepository(AgentRepository):
    """Keeps the final draft of each run: the service drops it when the run ends."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.snapshots: dict[uuid.UUID, dict[str, str | None]] = {}

    def drop_draft(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        self.snapshots[run_id] = {
            f.path: None if f.deleted else f.content for f in self.draft(project_id, run_id)
        }
        super().drop_draft(project_id, run_id)


def default_checker(settings: Settings) -> StaticChecker:
    return StaticChecker(
        temp_parent=settings.agent_check_temp_dir,
        timeout=settings.agent_check_timeout_seconds,
        tsc_path=settings.agent_tsc_path,
    )


def agent_service(
    session: Session,
    provider: LLMProvider,
    settings: Settings,
    checker: Checker,
    agents: AgentRepository,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> AgentService:
    """As `api.deps.make_agent_service`, but bound to the evaluation session and settings.

    Tests never run (no execution provider) and no embedding provider is used.
    """
    return AgentService(
        provider,
        patch_service(session, settings),
        agents,
        PatchRepository(session),
        PlanRepository(session),
        ToolContext(sessionmaker(bind=session.get_bind(), expire_on_commit=False)),
        checker,  # type: ignore[arg-type]  # structural: only .run is used
        limits=PatchLimits(
            settings.patch_max_files, settings.patch_max_file_bytes, settings.patch_max_patch_bytes
        ),
        config=AgentConfig(
            max_steps=settings.agent_max_steps,
            token_budget=settings.agent_token_budget,
            time_limit_seconds=settings.agent_time_limit_seconds,
            step_max_output_tokens=settings.agent_step_max_output_tokens,
            context_chars=settings.agent_context_chars,
            last_results=settings.agent_context_last_results,
        ),
        clock=clock,
        tests=TestRunner(
            UnavailableExecutionProvider(),
            timeout=settings.agent_test_timeout_seconds,
            output_cap=settings.agent_test_output_cap,
        ),
        embedder=None,
    )


def count_removals(baseline: Mapping[str, str], final: Mapping[str, str | None]) -> tuple[int, int]:
    """(definitions removed from modified accepted files, accepted files deleted).

    The first mirrors the agent's removal_check hook, which only looks at modified files.
    """
    removed = deleted = 0
    for path, old in baseline.items():
        new = final.get(path)
        if new is None:
            deleted += 1
        elif new != old:
            removed += len(removed_from_accepted(path, old, new))
    return removed, deleted


def _not_started(
    deployment: str, repeat: int, task_key: str, provider: LLMProvider, exc: Exception
) -> RunMetrics:
    return RunMetrics(
        deployment, repeat, task_key, "", provider.provider_name, provider.model_name,
        "not_started", "", 0, 0, 0, None, 0, 0, 0, 0, 0, 0, 0, 0, 0, "", "", type(exc).__name__,
    )  # fmt: skip


def run_once(
    session: Session,
    settings: Settings,
    seeded: SeededProject,
    task_key: str,
    deployment: str,
    repeat: int,
    provider: LLMProvider,
    checker: Checker,
    *,
    max_steps: int | None = None,
) -> RunMetrics:
    agents = SnapshotAgentRepository(session)
    service = agent_service(session, provider, settings, checker, agents)
    pid = seeded.project_id
    try:
        run = service.start_run(
            pid,
            seeded.plan_id,
            task_key,
            acknowledge_blocking_questions=True,
            max_steps=max_steps,
            deployment=deployment,
        )
    except Exception as exc:
        return _not_started(deployment, repeat, task_key, provider, exc)
    service.execute(pid, run.id)  # never raises; always releases the agent lock
    session.expire_all()
    run = service.get_run(pid, run.id)
    reasoning: int | None = None
    model_latency = 0
    for step in service.steps(pid, run.id):
        call: dict[str, Any] = (step.args or {}).get("call") or {}
        if call.get("reasoning_tokens") is not None:
            reasoning = (reasoning or 0) + int(call["reasoning_tokens"])
        model_latency += int(call.get("latency_ms") or 0)

    snapshot = agents.snapshots.get(run.id)
    if snapshot is None:  # the draft was never dropped: measure the accepted workspace
        snapshot = dict(seeded.baseline)
    live = {path: content for path, content in snapshot.items() if content is not None}
    task = next(t for t in PlanRepository(session).tasks(pid, seeded.plan_id) if t.key == task_key)
    coverage = patch_service(session, settings).coverage(pid, task, live)
    report = checker.run(live)
    removed, deleted = count_removals(seeded.baseline, snapshot)
    return RunMetrics(
        deployment=deployment,
        repeat=repeat,
        task_key=task_key,
        run_id=str(run.id),
        provider=run.provider,
        model=run.model,
        status=run.status,
        stop_reason=run.stop_reason or "",
        steps=run.steps_taken,
        prompt_tokens=run.input_tokens,
        completion_tokens=run.output_tokens,
        reasoning_tokens=reasoning,
        total_tokens=run.input_tokens + run.output_tokens,
        latency_ms=run.duration_ms or 0,
        model_latency_ms=model_latency,
        coverage_covered=int(coverage["covered"]),
        coverage_missing=int(coverage["missing"]),
        coverage_unchecked=int(coverage["unchecked"]),
        removals_from_accepted=removed,
        deleted_accepted_files=deleted,
        static_findings=report.total,
        static_unavailable=";".join(report.unavailable),
        patch_id=str(run.patch_id) if run.patch_id else "",
        error="",
    )


def run_eval(
    engine: Engine,
    settings: Settings,
    task_key: str,
    deployments: Sequence[str],
    repeats: int,
    provider_factory: ProviderFactory,
    checker: Checker,
    *,
    max_steps: int | None = None,
    progress: Callable[[RunMetrics], None] | None = None,
) -> list[RunMetrics]:
    """Each run gets its own freshly seeded project, so runs never see each other's work."""
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    rows: list[RunMetrics] = []
    for deployment in deployments:
        provider = provider_factory(deployment)
        for repeat in range(1, repeats + 1):
            with Session(engine, expire_on_commit=False) as session:
                seeded = seed_project(session, settings, f"Atlas eval {deployment} #{repeat}")
                if task_key not in seeded.task_keys:
                    raise ValueError(
                        f"Unknown task {task_key}; the plan has {', '.join(seeded.task_keys)}"
                    )
                row = run_once(
                    session, settings, seeded, task_key, deployment, repeat, provider, checker,
                    max_steps=max_steps,
                )  # fmt: skip
            rows.append(row)
            if progress:
                progress(row)
    return rows
