import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from app.domain.agent import (
    AGENT_PROMPT_VERSION,
    CONVERGENCE_LIMIT,
    MAX_NOTE_FINDINGS,
    MAX_SUMMARY_CHARS,
    REPEAT_LIMIT,
    RESPONSE_CHARS_PER_TOKEN,
    Action,
    ActionError,
    ContextInput,
    Draft,
    DraftFile,
    FinishArgs,
    StepView,
    build_context,
    cut,
    dump,
    parse_action,
    write_problem,
)
from app.domain.architecture import safe_finish_reason
from app.domain.checklist import Step as ChecklistStep
from app.domain.ci_loop import GREEN, PATCH_ON_STOP, Batch, protected
from app.domain.execution import (
    DEFAULT_OUTPUT_CAP,
    DEFAULT_TIMEOUT,
    ProviderUnavailableError,
    UnavailableExecutionProvider,
    UnknownCommandError,
    check_command,
)
from app.domain.patch import (
    Failure,
    GeneratedPatch,
    PatchInvalidError,
    PatchLimits,
    content_hash,
    requirement_code,
    stale_failures,
    validate_patch,
)
from app.embeddings.contracts import EmbeddingProvider
from app.llm.agent_prompt import build_prompt, build_system_prompt
from app.llm.contracts import LLMProvider, LLMRequest
from app.llm.exceptions import LLMReasoningBudgetExhaustedError
from app.mcp import tools
from app.models.agent import AgentJob, AgentRun, AgentStep
from app.models.plan import ImplementationPlan, PlanTask
from app.repositories.agent import AgentRepository
from app.repositories.checklist import ChecklistRepository
from app.repositories.patch import PatchRepository
from app.services.agent_hooks import HookInput, get_hooks
from app.services.agent_tools import AgentCall, draft_signature, static_check_data
from app.services.ci_execution import CIError, _scrub, check_ci_branch
from app.services.ci_loop import (
    APPROVAL,
    CHECKPOINT,
    CLEANUP,
    ITERATION,
    LOOP_VARIANTS,
    PROTECTION_NOTICE,
    CILoop,
    CILoopError,
    LoopRun,
    Validation,
    add_row,
    approval_record,
    cancel_loop,
    chain,
    classify_counts,
    grant_live,
    latest_checkpoint,
    loop_state,
    release_loop,
    revoke_live,
    rows,
    update_row,
)
from app.services.execution import TestRunner, cancel_run_jobs, release_run
from app.services.hooks import Point
from app.services.patch import PatchBusyError, PatchService, TaskNotFoundError
from app.services.plan import PlanNotFoundError
from app.services.project import ProjectNotFoundError
from app.services.static_checks import StaticChecker
from app.tools import Tool, get_registry

logger = logging.getLogger(__name__)

# One agent run at a time per process: bounds provider cost and temporary-directory use.
_agent_lock = threading.Lock()
_active: set[uuid.UUID] = set()
_MAX_BRIEF_REQUIREMENTS = 20
_GRAPH_ROOTS = 11  # the task's item and up to 10 of its requirements
_GRAPH_HITS_PER_ROOT = 8
GRAPH_LABEL = "related items (from the graph)"


class RunNotFoundError(Exception):
    def __init__(self, run_id: uuid.UUID | str) -> None:
        super().__init__(f"Agent run {run_id} not found")


class RunNotRunningError(Exception):
    pass


class HookNotFoundError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown hook: {name[:60]}")


class HookRequiredError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"The hook {name} cannot be disabled")


class CheckpointStaleError(Exception):
    """The accepted workspace changed under files the checkpoint changed (HTTP 409)."""

    def __init__(self, paths: Sequence[str]) -> None:
        self.paths = sorted(paths)
        super().__init__(
            "The accepted workspace changed since this checkpoint was saved; restart the step "
            "instead. Changed: " + ", ".join(self.paths[:20])
        )


@dataclass(frozen=True)
class StepRun:
    """What a checklist step run uses instead of the whole task."""

    criteria: list[str]
    note: str
    brief: dict[str, Any]


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int
    token_budget: int
    time_limit_seconds: float
    step_max_output_tokens: int
    context_chars: int
    last_results: int


@dataclass
class _CIRun:
    """The CI repair loop of one running checklist step run (Phase 13E)."""

    loop: LoopRun
    batch: Batch
    check_cache: dict[str, Any]
    steps: list[StepView]
    call: AgentCall | None = None
    auto_finished: str | None = None  # the draft signature an automatic finish was tried for


# Agent stop reasons as CI loop terminal conditions.
_CI_REASONS = {"user_stop": "cancelled", "token_budget": "token_limit"}
_CI_FAILED = frozenset({"ci_unavailable", "reasoning_budget_exhausted"})


def is_active(run_id: uuid.UUID) -> bool:
    return run_id in _active


class AgentService:
    def __init__(
        self,
        provider: LLMProvider,
        patches: PatchService,
        agents: AgentRepository,
        patch_repo: PatchRepository,
        plans: Any,
        tool_context: tools.ToolContext,
        checker: StaticChecker,
        *,
        limits: PatchLimits,
        config: AgentConfig,
        clock: Callable[[], float] = time.monotonic,
        tests: TestRunner | None = None,
        embedder: EmbeddingProvider | None = None,
        checklists: ChecklistRepository | None = None,
        ci_loop: CILoop | None = None,
    ) -> None:
        self._ci_loop = ci_loop
        self._ci: dict[uuid.UUID, _CIRun] = {}
        self._embedder = embedder
        self._checklists = checklists
        self._step_runs: dict[uuid.UUID, StepRun] = {}
        self._provider = provider
        self._tests = tests or TestRunner(
            UnavailableExecutionProvider(),
            timeout=DEFAULT_TIMEOUT,
            output_cap=DEFAULT_OUTPUT_CAP,
        )
        self._patches = patches
        self._agents = agents
        self._patch_repo = patch_repo
        self._plans = plans
        self._tools = tool_context
        self._checker = checker
        self._limits = limits
        self._config = config
        self._clock = clock

    # ---- queries ---------------------------------------------------------------------------

    def get_run(self, project_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
        run = self._agents.get_run(project_id, run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def steps(self, project_id: uuid.UUID, run_id: uuid.UUID) -> list[AgentStep]:
        return list(self._agents.steps(project_id, run_id))

    def jobs(self, project_id: uuid.UUID, run_id: uuid.UUID) -> list[AgentJob]:
        """Test jobs; the CI loop's bookkeeping rows are shown through ci_view instead."""
        return [j for j in self._agents.jobs(project_id, run_id) if j.variant not in LOOP_VARIANTS]

    def runs_for_task(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> list[AgentRun]:
        _, task, _ = self._patches._plan_and_task(project_id, plan_id, task_key)
        return list(self._agents.runs_for_task(project_id, task.id))

    def stop(self, project_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
        run = self.get_run(project_id, run_id)
        if run.status != "running":
            raise RunNotRunningError("The run has already ended")
        self._agents.request_stop(project_id, run_id)
        cancel_run_jobs(run_id)
        # Stop also ends the CI loop: polling wakes at once and the approval is revoked.
        cancel_loop(run_id)
        self._revoke_approval(project_id, run_id, "stopped")
        return self.get_run(project_id, run_id)

    def mark_interrupted(self, project_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun:
        """A run still 'running' whose worker is gone (for example after a restart)."""
        run = self.get_run(project_id, run_id)
        if run.status == "running" and run_id not in _active:
            self._finish_row(run, "failed", "interrupted", None)
        return run

    # ---- start -----------------------------------------------------------------------------

    def start_run(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_key: str,
        *,
        acknowledge_blocking_questions: bool = False,
        max_steps: int | None = None,
        deployment: str | None = None,
        checklist_id: uuid.UUID | None = None,
        step_key: str | None = None,
        resume_from: uuid.UUID | None = None,
        discard_checkpoint: uuid.UUID | None = None,
    ) -> AgentRun:
        plan, task, _ = self._patches.check_gates(
            project_id,
            plan_id,
            task_key,
            acknowledge_blocking_questions,
            checklist_step=checklist_id is not None,
        )
        overlay = self.checkpoint_overlay(project_id, resume_from) if resume_from else []
        if resume_from and checklist_id is not None and self._ci_loop is not None:
            checklist = self._checklists.get(project_id, checklist_id) if self._checklists else None
            self._ci_loop.check_resume(
                self._agents,
                project_id,
                resume_from,
                checklist_id=checklist_id,
                checklist_version=checklist.version if checklist is not None else None,
            )
        if not _agent_lock.acquire(blocking=False):
            raise PatchBusyError("An agent run is already in progress")
        try:
            run = AgentRun(
                id=uuid.uuid4(),
                hooks=self.hook_states(project_id),
                project_id=project_id,
                plan_id=plan.id,
                task_id=task.id,
                task_key=task.key,
                status="running",
                blocking_acknowledged=acknowledge_blocking_questions
                and bool(task.blocking_questions),
                max_steps=min(max_steps or self._config.max_steps, self._config.max_steps),
                token_budget=self._config.token_budget,
                time_limit_seconds=self._config.time_limit_seconds,
                provider=self._provider.provider_name,
                model=deployment or self._provider.model_name,
                deployment=deployment or self._provider.model_name,
                prompt_version=AGENT_PROMPT_VERSION,
                checklist_id=checklist_id,
                checklist_step_key=step_key,
                resumed_from_run_id=resume_from,
            )
            run = self._agents.add_run(
                run,
                self._patch_repo.workspace(project_id),
                overlay,
                retire=resume_from or discard_checkpoint,
            )
        except Exception:
            _agent_lock.release()
            raise
        _active.add(run.id)
        return run

    def checkpoint_overlay(
        self, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> list[tuple[str, str, str | None, bool]]:
        """The changed files of a run's saved draft, to apply on top of the current workspace.

        Raises CheckpointStaleError when an accepted file the checkpoint changed (or a path it
        created) changed in the workspace since; the person restarts the step then.
        """
        current = self._patch_repo.workspace_hashes(project_id)
        out: list[tuple[str, str, str | None, bool]] = []
        stale: list[str] = []
        for f in self._agents.draft(project_id, run_id):
            if f.base_hash is None:
                if f.path in current:
                    stale.append(f.path)
                else:
                    out.append((f.path, f.content, None, f.deleted))
                continue
            changed = f.deleted or content_hash(f.content) != f.base_hash
            if not changed:
                continue
            if current.get(f.path) != f.base_hash:
                stale.append(f.path)
            else:
                out.append((f.path, f.content, f.base_hash, f.deleted))
        if stale:
            raise CheckpointStaleError(stale)
        return out

    # ---- loop ------------------------------------------------------------------------------

    def execute(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Run the loop of a started run to its end. Never raises; the lock is always released."""
        try:
            self._tests.bind(project_id, run_id)
            self._loop(project_id, run_id)
        except Exception as exc:
            # The type only: messages of failed steps may carry draft content.
            logger.error("agent run failed run=%s error=%s", run_id, type(exc).__name__)
            try:
                self._agents.rollback()
                run = self._agents.get_run(project_id, run_id)
                if run is not None and run.status == "running":
                    self._finish_row(run, "failed", "internal_error", None)
            except Exception as inner:
                logger.error("agent run not closed run=%s error=%s", run_id, type(inner).__name__)
        finally:
            release_run(run_id)
            release_loop(run_id)
            self._ci.pop(run_id, None)
            self._tests.release(run_id)
            _active.discard(run_id)
            _agent_lock.release()

    def _finish_row(
        self,
        run: AgentRun,
        status: str,
        reason: str,
        summary: str | None,
        duration_ms: int | None = None,
    ) -> None:
        now = self._agents.now()
        if duration_ms is None:
            started = run.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            duration_ms = max(int((now - started).total_seconds() * 1000), 0)
        run.status = status
        run.stop_reason = reason
        run.summary = summary
        run.finished_at = now
        run.duration_ms = duration_ms
        # A failed or stopped checklist step run keeps its draft as the checkpoint to resume from.
        keep = run.checklist_id is not None and status in ("failed", "stopped") and not run.patch_id
        if not keep:
            self._agents.drop_draft(run.project_id, run.id)
        self._step_runs.pop(run.id, None)
        self._agents.commit()
        logger.info(
            "agent run ended run=%s status=%s reason=%s steps=%d",
            run.id,
            status,
            reason,
            run.steps_taken,
        )

    def _end(
        self, run: AgentRun, status: str, reason: str, elapsed: float, summary: str | None = None
    ) -> None:
        self._finish_row(run, status, reason, summary, int(elapsed * 1000))

    def _limit(self, run: AgentRun, elapsed: float) -> str | None:
        if run.stop_requested:
            return "user_stop"
        if run.steps_taken >= run.max_steps:
            return "step_limit"
        if run.input_tokens + run.output_tokens >= run.token_budget:
            return "token_budget"
        if elapsed >= run.time_limit_seconds:
            return "time_limit"
        return None

    def _loop(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        run = self.get_run(project_id, run_id)
        plan = self._plans.get(project_id, run.plan_id)
        tasks = self._plans.tasks(project_id, run.plan_id)
        task = next((t for t in tasks if t.id == run.task_id), None)
        if plan is None or task is None:
            raise TaskNotFoundError(run.task_key)
        warnings = self._patches.dependency_warnings(project_id, plan, task, tasks)
        draft = Draft(
            [
                DraftFile(f.path, f.content, f.base_hash, f.deleted)
                for f in self._agents.draft(project_id, run_id)
            ]
        )
        brief = self._brief(project_id, task, run.blocking_acknowledged)
        brief[GRAPH_LABEL] = self.graph_context(project_id, task)
        step_run = self._step_run(project_id, run, draft)
        if step_run is not None:
            self._step_runs[run.id] = step_run
            brief.update(step_run.brief)
        decision_block, brief["flags"] = self._patches.decision_context(project_id, plan, task)
        feedback, based_on = self._patches.feedback_context(project_id, task)
        framework_rules = self._patches.frameworks(project_id, draft.live()).context()
        if framework_rules:
            brief["framework_rules"] = framework_rules
        repository = self.repository_context(project_id, task)
        if repository is not None:
            brief["repository"] = repository
        tests_ok, _ = self._tests.status()
        if tests_ok:
            brief["run_tests"] = "available: pytest, ruff, mypy, tsc, npm test"
        check_cache: dict[str, Any] = {}
        steps: list[StepView] = []
        started = self._clock()
        ci = self._ci_start(project_id, run, check_cache, steps, started)
        if ci is not None:
            config = ci.loop.loop.config
            brief["run_tests"] = f"runs in GitHub Actions CI ({config.command})"
            brief["ci_loop"] = (
                "Your edits are tested in the repository's CI, in batches: the draft is pushed "
                "and tested when you call run_tests or finish, when the static checks after an "
                f"edit find nothing, or after {config.max_edits} edits. You then see new "
                "failures in changed files (or linked to them), flaky tests and counts of the "
                f"rest. At most {config.max_iterations} CI iterations. finish succeeds only "
                "when CI is green for the current draft."
            )
        offered = self.offered_tools(ci=ci is not None)
        system_prompt = build_system_prompt(offered)
        tool_models = {t.name: t.args_model for t in self._agent_tools().values()}
        disabled_hooks = (
            frozenset(h["name"] for h in run.hooks if not h.get("enabled"))
            if run.hooks is not None
            else frozenset(self._agents.disabled_hooks(project_id))
        )
        total_latency = 0
        previous: tuple[str, str | None, str | None] | None = None
        repeats = 0
        best_total: int | None = None
        stalled = 0
        check_totals: list[int] = []
        last_findings: list[dict[str, Any]] = []
        while True:
            self._agents.refresh(run)
            elapsed = self._clock() - started
            reason = self._limit(run, elapsed)
            if ci is not None:
                if reason is None and ci.loop.revoked():
                    reason = "approval_revoked"
                if reason is not None:
                    self._ci_terminal(
                        ci, _CI_REASONS.get(reason, reason), project_id, plan, task, draft, run,
                        warnings, total_latency, based_on, started,
                    )  # fmt: skip
                    return
            if reason:
                self._end(run, "stopped", reason, elapsed)
                return
            step_no = run.steps_taken + 1
            changes = draft.changes()
            context = build_context(
                ContextInput(
                    brief=brief,
                    decisions=decision_block,
                    feedback=feedback,
                    steps=steps,
                    step_no=step_no,
                    max_steps=run.max_steps,
                    tokens_used=run.input_tokens + run.output_tokens,
                    token_budget=run.token_budget,
                    elapsed_seconds=elapsed,
                    time_limit_seconds=run.time_limit_seconds,
                    changed=[{"path": c.path, "change": c.change} for c in changes],
                    path_count=len(draft.live()),
                ),
                self._config.context_chars,
                self._config.last_results,
            )
            try:
                response = self._provider.generate(
                    LLMRequest(
                        prompt=build_prompt(context, uuid.uuid4().hex[:16]),
                        system_prompt=system_prompt,
                        max_output_tokens=self._config.step_max_output_tokens,
                        temperature=0,
                    )
                )
            except LLMReasoningBudgetExhaustedError:
                logger.error("agent provider reasoning budget exhausted run=%s", run_id)
                if ci is not None:
                    run.summary = "Reasoning budget exhausted: raise the deployment's budget."
                    self._ci_terminal(
                        ci, "reasoning_budget_exhausted", project_id, plan, task, draft, run,
                        warnings, total_latency, based_on, started,
                    )  # fmt: skip
                    return
                self._end(
                    run,
                    "failed",
                    "reasoning_budget_exhausted",
                    self._clock() - started,
                    "Reasoning budget exhausted: raise the completion budget of this deployment.",
                )
                return
            except Exception as exc:
                logger.error("agent provider error run=%s error=%s", run_id, type(exc).__name__)
                self._end(run, "failed", "provider_error", self._clock() - started)
                return
            used_in = response.usage.input_tokens or 0
            used_out = response.usage.output_tokens or 0
            model_call = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
                "finish_reason": response.finish_reason,
                "latency_ms": response.latency_ms,
            }
            run.input_tokens += used_in
            run.output_tokens += used_out
            total_latency += response.latency_ms
            run.provider, run.model = response.provider, response.model
            action_name, path, args = "invalid", None, {}
            result: dict[str, Any]
            error: ActionError | None = None
            done = False
            hook_log: list[dict[str, str]] = []
            try:
                action = parse_action(
                    response.text,
                    finish_reason=safe_finish_reason(response.finish_reason),
                    max_chars=self._config.step_max_output_tokens * RESPONSE_CHARS_PER_TOKEN,
                    arg_models=tool_models,
                )
                action_name = action.name
                path = cut(str(getattr(action.args, "path", "") or ""), 300) or None
                tool = get_registry().get("agent", action.name)
                args = tool.log_args(action.args) if tool.log_args else {}
                result, done = self._act(
                    action,
                    project_id,
                    plan,
                    task,
                    tasks,
                    draft,
                    run,
                    warnings,
                    total_latency,
                    response.provider,
                    response.model,
                    based_on,
                    hook_log=hook_log,
                    disabled_hooks=disabled_hooks,
                    check_cache=check_cache,
                )
            except ActionError as exc:
                error = exc
                path = path or (cut(exc.path, 300) if exc.path else None)
                result = {"error": exc.code, "message": str(exc), **exc.data}
            if ci is not None and error is None and not done:
                feed = self._ci_after_action(ci, action_name, draft)
                if feed is not None:
                    result["ci"] = feed
            args = {**args, "call": model_call}
            if hook_log:
                args = {**args, "hooks": hook_log}
            summary = cut(
                str(error) if error else str(result.pop("summary", "ok")), MAX_SUMMARY_CHARS
            )
            text = dump(result)
            self._save_draft(project_id, run.id, draft)
            self._agents.add_step(
                AgentStep(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    project_id=project_id,
                    step_no=step_no,
                    action=action_name,
                    path=path,
                    args=args,
                    status="error" if error else "ok",
                    error_code=error.code if error else None,
                    result_summary=summary,
                    result=text,
                    input_tokens=used_in,
                    output_tokens=used_out,
                    latency_ms=response.latency_ms,
                )
            )
            run.steps_taken = step_no
            self._agents.commit()
            steps.append(
                StepView(
                    step_no,
                    action_name,
                    path,
                    "error" if error else "ok",
                    error.code if error else None,
                    summary,
                    text,
                )
            )
            if done:
                reason = GREEN if ci is not None else "finished"
                self._end(run, "finished", reason, self._clock() - started, run.summary)
                return
            if ci is not None:
                if ci.loop.terminal is not None:
                    self._ci_terminal(
                        ci, ci.loop.terminal, project_id, plan, task, draft, run, warnings,
                        total_latency, based_on, started,
                    )  # fmt: skip
                    return
                if self._ci_auto_finish(
                    ci, project_id, plan, task, tasks, draft, run, warnings, total_latency,
                    based_on, disabled_hooks,
                ):  # fmt: skip
                    self._end(run, "finished", GREEN, self._clock() - started, run.summary)
                    return
            signature = (action_name, error.code if error else None, path)
            if error is None:
                previous, repeats = None, 0
            elif signature == previous:
                repeats += 1
            else:
                previous, repeats = signature, 1
            if error is not None and error.code == "checks_unavailable":
                self._end(run, "stopped", "checks_unavailable", self._clock() - started)
                return
            if repeats >= REPEAT_LIMIT:
                self._end(run, "stopped", "repeated_error", self._clock() - started)
                return
            counted: dict[str, Any] | None = None
            if check_cache.pop("fresh", False):
                counted = check_cache.get("data")
            elif action_name == "run_static_checks" and error is None:
                counted = None if check_cache.pop("served", False) else result
            else:
                check_cache.pop("served", None)
            if counted is not None and counted.get("total"):
                total = int(counted["total"])
                check_totals.append(total)
                last_findings = list(counted.get("findings") or [])
                if best_total is None or total < best_total:
                    best_total, stalled = total, 0
                else:
                    stalled += 1
                if stalled >= CONVERGENCE_LIMIT:
                    self._stop_not_converging(
                        project_id, plan, task, draft, run, warnings, total_latency,
                        check_totals, last_findings, based_on,
                    )  # fmt: skip
                    self._end(
                        run, "stopped", "not_converging", self._clock() - started, run.summary
                    )
                    return
            elif counted is not None:
                best_total, stalled = 0, 0

    def _save_draft(self, project_id: uuid.UUID, run_id: uuid.UUID, draft: Draft) -> None:
        for path in sorted(draft.dirty):
            file = draft.files.get(path)
            if file is None:
                self._agents.remove_draft_file(run_id, path)
            else:
                self._agents.save_draft_file(
                    project_id, run_id, path, file.content, file.base_hash, file.deleted
                )
        draft.dirty.clear()

    # ---- CI repair loop (Phase 13E) --------------------------------------------------------

    def _ci_mode(self, project_id: uuid.UUID, run: AgentRun) -> bool:
        return (
            self._ci_loop is not None
            and run.checklist_id is not None
            and self._ci_loop.available(project_id)
        )

    def _checklist_version(self, project_id: uuid.UUID, run: AgentRun) -> int | None:
        if run.checklist_id is None or self._checklists is None:
            return None
        found = self._checklists.get(project_id, run.checklist_id)
        return found.version if found is not None else None

    def _ci_start(
        self,
        project_id: uuid.UUID,
        run: AgentRun,
        check_cache: dict[str, Any],
        steps: list[StepView],
        started: float,
    ) -> _CIRun | None:
        if self._ci_loop is None or not self._ci_mode(project_id, run):
            return None
        loop = self._ci_loop.start(
            self._agents,
            run,
            clock=self._clock,
            deadline=started + run.time_limit_seconds,
            checklist_version=self._checklist_version(project_id, run),
        )
        ci = _CIRun(loop, Batch(self._ci_loop.config.max_edits), check_cache, steps)
        self._ci[run.id] = ci
        return ci

    def _ci_after_action(self, ci: _CIRun, action_name: str, draft: Draft) -> dict[str, Any] | None:
        """An edit joins the batch; a full or statically clean batch is validated in CI."""
        cache = ci.check_cache
        static = None
        if cache.get("fresh") and cache.get("sig") == draft_signature(draft):
            static = int((cache.get("data") or {}).get("total") or 0)
        if ci.batch.after(action_name, True, static) is None:
            return None
        try:
            return self._ci_validate(ci, draft).feed
        except ActionError as exc:
            return {"status": exc.code, "message": str(exc)}

    def _ci_static(self, ci: _CIRun, draft: Draft) -> dict[str, Any]:
        cache = ci.check_cache
        if cache.get("sig") == draft_signature(draft) and isinstance(cache.get("data"), dict):
            return dict(cache["data"])
        assert ci.call is not None
        return static_check_data(ci.call)  # ActionError("checks_unavailable") goes to the model

    def _ci_validate(self, ci: _CIRun, draft: Draft) -> Validation:
        static = self._ci_static(ci, draft)
        total = int(static.get("total") or 0)
        changes = draft.changes()
        validation = ci.loop.validate(
            files={p: f.content for p, f in draft.live().items()},
            draft_sig=draft_signature(draft),
            static_total=total,
            summaries=[f"{s.step_no} {s.action} {s.summary}" for s in ci.steps],
            file_hashes={
                c.path: None if c.change == "delete" else content_hash(c.content) for c in changes
            },
        )
        if validation.status == "static_failed":
            validation.feed["findings"] = list(static.get("findings") or [])[:MAX_NOTE_FINDINGS]
        ci.batch.reset()
        return validation

    def _ci_auto_finish(
        self,
        ci: _CIRun,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        tasks: Any,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
        latency_ms: int,
        based_on: int | None,
        disabled_hooks: frozenset[str],
    ) -> bool:
        """Green for the current draft: finish without another model call (once per draft).
        A refusal by a finish gate is a step the model sees; the loop goes on."""
        signature = draft_signature(draft)
        if ci.loop.green_sig != signature or ci.auto_finished == signature:
            return False
        ci.auto_finished = signature
        hook_log: list[dict[str, str]] = []
        summary = f"CI green after {ci.loop.iteration} iteration(s)"
        error: ActionError | None = None
        done = False
        try:
            result, done = self._act(
                Action("finish", FinishArgs(summary=summary)), project_id, plan, task, tasks,
                draft, run, warnings, latency_ms, run.provider, run.model, based_on,
                hook_log=hook_log, disabled_hooks=disabled_hooks, check_cache=ci.check_cache,
            )  # fmt: skip
        except ActionError as exc:
            error = exc
            result = {"error": exc.code, "message": str(exc), **exc.data}
        step_no = run.steps_taken + 1
        text = dump(result)
        short = cut(str(error) if error else str(result.pop("summary", "ok")), MAX_SUMMARY_CHARS)
        self._save_draft(project_id, run.id, draft)
        self._agents.add_step(
            AgentStep(
                id=uuid.uuid4(),
                run_id=run.id,
                project_id=project_id,
                step_no=step_no,
                action="finish",
                path=None,
                args={"auto": "ci_green", **({"hooks": hook_log} if hook_log else {})},
                status="error" if error else "ok",
                error_code=error.code if error else None,
                result_summary=short,
                result=text,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
            )
        )
        run.steps_taken = step_no
        self._agents.commit()
        ci.steps.append(
            StepView(
                step_no, "finish", None, "error" if error else "ok",
                error.code if error else None, short, text,
            )
        )  # fmt: skip
        return done

    def _ci_terminal(
        self,
        ci: _CIRun,
        reason: str,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
        latency_ms: int,
        based_on: int | None,
        started: float,
    ) -> None:
        """A non-green end. A reviewable patch only when deterministic validation passes and
        the stop is one to review (not a cancel, a revoke or an unavailable CI)."""
        loop = ci.loop
        if loop.terminal is None:
            loop.terminal = reason
        run.summary = cut(f"CI loop stopped: {reason}. {loop.message or ''}".strip(), 1000)
        if reason in PATCH_ON_STOP and draft.changes():
            call = AgentCall(
                self, project_id, plan, task, draft, run, warnings, latency_ms, run.provider,
                run.model, based_on,
            )  # fmt: skip
            try:
                valid = not self.draft_failures(call) and not static_check_data(call)["total"]
            except ActionError:
                valid = False
            if valid:
                try:
                    self._propose_draft(
                        project_id, plan, task, draft, run, warnings, latency_ms, run.provider,
                        run.model, loop.stop_notes(reason), [], [], based_on,
                    )  # fmt: skip
                except ActionError as exc:
                    logger.warning("ci loop draft not proposed run=%s code=%s", run.id, exc.code)
        status = "failed" if reason in _CI_FAILED else "stopped"
        self._end(run, status, reason, self._clock() - started, run.summary)

    def _revoke_approval(self, project_id: uuid.UUID, run_id: uuid.UUID, how: str) -> bool:
        """Revokes the run's CI approval, or denies a pending one. True when one was active."""
        live = loop_state(project_id, run_id)
        waiting = live is not None and live["approval"] in ("pending", "approved")
        revoke_live(project_id, run_id)
        found = approval_record(self._agents, project_id, run_id)
        now = self._agents.now().isoformat()
        if found is None:
            if waiting:  # a denial is final too: the run cannot be approved afterwards
                add_row(
                    self._agents, project_id, run_id, APPROVAL,
                    {
                        "project_id": str(project_id), "run_id": str(run_id),
                        "approved_by": None, "approved_at": None, "revoked_at": now,
                        "revoked": how,
                    },
                )  # fmt: skip
            return waiting
        if (found.summary or {}).get("revoked_at"):
            return waiting
        update_row(self._agents, found, revoked_at=now, revoked=how)
        return True

    def _ci_run(self, project_id: uuid.UUID, run_id: uuid.UUID) -> tuple[AgentRun, CILoop]:
        run = self.get_run(project_id, run_id)
        if self._ci_loop is None or run.checklist_id is None:
            raise CILoopError("not_found", "This run has no CI repair loop")
        return run, self._ci_loop

    def ci_approval_info(self, project_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
        """What the person approves: shown before the approval, never the token."""
        run, loop = self._ci_run(project_id, run_id)
        try:
            target = loop.target(project_id)
        except ProviderUnavailableError as exc:
            raise CILoopError("ci_unavailable", str(exc)) from None
        branch = loop.branch_for(self._agents, project_id, run.id)
        found = approval_record(self._agents, project_id, run.id)
        return {
            "run_id": str(run.id),
            "coding_step": run.checklist_step_key,
            "repository": f"{target.owner}/{target.repo}",
            "remote": target.html_repo,
            "branch": branch,
            "base_branch": target.base_branch,
            "workflow": loop.config.workflow,
            "command": loop.config.command,
            "max_iterations": loop.config.max_iterations,
            "protected_notice": PROTECTION_NOTICE,
            "approved": found is not None,
        }

    def ci_approve(
        self, project_id: uuid.UUID, run_id: uuid.UUID, approved_by: str
    ) -> dict[str, Any]:
        """One explicit approval for this run and its exact CI branch; never extended or
        reused. Only a person calls this (the API); no agent or MCP tool reaches it."""
        run, loop = self._ci_run(project_id, run_id)
        if run.status != "running":
            raise CILoopError("not_running", "The run has ended")
        if approval_record(self._agents, project_id, run.id) is not None:
            raise CILoopError("already_decided", "The CI branch of this run was already decided")
        info = self.ci_approval_info(project_id, run_id)
        target = loop.target(project_id)
        try:
            check_ci_branch(
                info["branch"],
                prefix=target.branch_prefix,
                base_branch=target.base_branch,
                default_branch=target.base_branch,
            )
        except CIError as exc:
            raise CILoopError("protected_branch", str(exc)) from None
        if protected(info["branch"], base_branch=target.base_branch, default_branch="main"):
            raise CILoopError("protected_branch", "A protected branch can never be approved")
        at = self._agents.now().isoformat()
        add_row(
            self._agents,
            project_id,
            run.id,
            APPROVAL,
            {
                "project_id": str(project_id),
                "run_id": str(run.id),
                "repository": info["repository"],
                "remote": info["remote"],
                "branch": info["branch"],
                "base_branch": info["base_branch"],
                "approved_by": approved_by,
                "approved_at": at,
                "revoked_at": None,
            },
        )
        grant_live(project_id, run.id, approved_by, at)
        logger.info("ci loop approved run=%s", run.id)
        return self.ci_view(project_id, run) or {}

    def ci_revoke(self, project_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
        run, _ = self._ci_run(project_id, run_id)
        if not self._revoke_approval(project_id, run.id, "revoked"):
            raise CILoopError("not_approved", "There is no active CI approval to revoke")
        logger.info("ci loop approval revoked run=%s", run.id)
        return self.ci_view(project_id, run) or {}

    def ci_delete_branch(
        self, project_id: uuid.UUID, run_id: uuid.UUID, *, confirm: bool
    ) -> dict[str, Any]:
        """Deletes the CI branch of an ended run on request; never while an open pull request
        uses it, and only with an explicit confirmation while a patch references it. Logs and
        parsed results stay."""
        run, loop = self._ci_run(project_id, run_id)
        if run.status == "running":
            raise CILoopError("not_ended", "The run is still running")
        checkpoint = latest_checkpoint(self._agents, project_id, run.id)
        branch = str((checkpoint or {}).get("branch") or "")
        if not branch:
            raise CILoopError("no_branch", "This run has no CI branch")
        ids = chain(self._agents, project_id, run.id)
        if rows(self._agents, project_id, ids, CLEANUP):
            raise CILoopError("already_deleted", "The CI branch was already deleted")
        try:
            target = loop.target(project_id)
            if loop.open_pulls(target, branch):
                raise CILoopError("in_use", "An open pull request uses the CI branch")
            if run.patch_id is not None and not confirm:
                raise CILoopError(
                    "confirm_required", "A proposed patch references the CI branch; confirm"
                )
            loop.delete_branch(target, branch)
        except ProviderUnavailableError as exc:
            raise CILoopError("ci_unavailable", str(exc)) from None
        except CIError as exc:
            raise CILoopError("ci_unavailable", _scrub(str(exc), target.token)) from None
        add_row(
            self._agents, project_id, run.id, CLEANUP,
            {"branch": branch, "deleted_at": self._agents.now().isoformat()},
        )  # fmt: skip
        return self.ci_view(project_id, run) or {}

    def ci_view(self, project_id: uuid.UUID, run: AgentRun) -> dict[str, Any] | None:
        """The run panel's CI loop block (None for a run without one)."""
        if self._ci_loop is None or run.checklist_id is None:
            return None
        live = loop_state(project_id, run.id)
        iterations = [
            dict(r.summary or {}) for r in rows(self._agents, project_id, [run.id], ITERATION)
        ]
        checkpoints = rows(self._agents, project_id, [run.id], CHECKPOINT)
        approval = approval_record(self._agents, project_id, run.id)
        if (
            live is None
            and not iterations
            and not checkpoints
            and approval is None
            and not self._ci_loop.available(project_id)
        ):
            return None
        tried = [i for i in iterations if not i.get("baseline")]
        latest = tried[-1] if tried else {}
        results = list(latest.get("results") or [])
        actionable = set(latest.get("actionable") or [])
        checkpoint = dict(checkpoints[-1].summary or {}) if checkpoints else {}
        record = dict(approval.summary or {}) if approval is not None else {}
        ids = chain(self._agents, project_id, run.id)
        branch = (live or {}).get("branch") or checkpoint.get("branch")
        if not branch:
            try:
                branch = self._ci_loop.branch_for(self._agents, project_id, run.id)
            except ProviderUnavailableError:
                branch = None
        status = "none"
        if record:
            status = "revoked" if record.get("revoked_at") else "approved"
        elif live is not None and live["approval"] == "pending":
            status = "pending"
        elapsed = (run.duration_ms or 0) / 1000
        if run.status == "running":
            began = run.started_at
            started = began if began.tzinfo else began.replace(tzinfo=UTC)
            elapsed = max((self._agents.now() - started).total_seconds(), 0.0)
        baseline = next((i for i in iterations if i.get("baseline")), None)
        return {
            "coding_step": run.checklist_step_key,
            "branch": branch,
            "approval": status,
            "approved_by": record.get("approved_by"),
            "approved_at": record.get("approved_at"),
            "phase": (live or {}).get("phase") or ("ended" if run.status != "running" else "idle"),
            "iteration": int(latest.get("iteration") or checkpoint.get("iteration") or 0),
            "max_iterations": self._ci_loop.config.max_iterations,
            "commit": (live or {}).get("commit")
            or latest.get("commit")
            or checkpoint.get("pushed_commit"),
            "workflow_status": (live or {}).get("workflow_status") or latest.get("conclusion"),
            "workflow_url": (live or {}).get("workflow_url") or latest.get("workflow_url"),
            "baseline_url": baseline.get("workflow_url") if baseline else None,
            "counts": classify_counts(latest),
            "actionable": [r for r in results if r.get("signature") in actionable][:50],
            "elsewhere": [
                r for r in results if r.get("novelty") == "new" and r.get("scope") == "elsewhere"
            ][:50],  # fmt: skip
            "flaky": [r for r in results if r.get("novelty") == "flaky"][:50],
            "green": bool(latest.get("green")),
            "terminal": run.stop_reason if run.status != "running" else None,
            "branch_deleted": bool(rows(self._agents, project_id, ids, CLEANUP)),
            "tokens_used": run.input_tokens + run.output_tokens,
            "token_budget": run.token_budget,
            "elapsed_seconds": round(elapsed, 1),
            "time_limit_seconds": run.time_limit_seconds,
            "message": (live or {}).get("message"),
        }

    # ---- checklist steps -------------------------------------------------------------------

    def _step_run(self, project_id: uuid.UUID, run: AgentRun, draft: Draft) -> StepRun | None:
        """The brief of a checklist step run: this step only, and what earlier steps added."""
        if run.checklist_id is None or self._checklists is None:
            return None
        checklist = self._checklists.get(project_id, run.checklist_id)
        if checklist is None:
            raise TaskNotFoundError(run.task_key)
        steps = [ChecklistStep.from_json(s) for s in checklist.steps]
        step = next((s for s in steps if s.key == run.checklist_step_key), None)
        if step is None:
            raise TaskNotFoundError(run.task_key)
        accepted: dict[str, list[str]] = {}
        for other in self._agents.runs_for_checklist(project_id, checklist.id):
            if other.patch_id is None or other.checklist_step_key in accepted:
                continue
            patch = self._patch_repo.get(project_id, other.patch_id)
            if patch is not None and patch.status == "accepted" and other.checklist_step_key:
                accepted[other.checklist_step_key] = self._patches.patch_files(project_id, patch.id)
        block: dict[str, Any] = {
            "key": step.key,
            "position": f"{[s.key for s in steps].index(step.key) + 1} of {len(steps)}",
            "title": step.title,
            "goal": step.goal,
            "expected_files": [f.as_json() for f in step.expected_files],
            "discovery_note": step.discovery_note or None,
            "depends_on": list(step.depends_on),
            "related_items": [r.as_json() for r in step.related_items],
            "accepted_earlier_steps": [
                {"key": s.key, "title": s.title, "files": accepted[s.key][:20]}
                for s in steps
                if s.key in accepted
            ],
            "note": "Work on this checklist step only: meet its acceptance criteria. Files "
            "accepted for earlier steps are already in the workspace; keep them. Later steps "
            "are done in later runs. Expected files are hints, not requirements.",
        }
        brief: dict[str, Any] = {
            "acceptance_criteria": [cut(c, 600) for c in step.acceptance_criteria],
            "checklist_step": block,
        }
        if run.resumed_from_run_id is not None:
            brief["checkpoint"] = {
                "resumed_from_run": str(run.resumed_from_run_id),
                "changed": [{"path": c.path, "change": c.change} for c in draft.changes()][:50],
                "note": "This run continues the saved draft of an earlier run of this step; the "
                "changed files are already in your draft. Check them and continue.",
            }
        return StepRun(
            criteria=list(step.acceptance_criteria),
            note=f"Checklist step {step.key}: {step.title}",
            brief=brief,
        )

    def _criteria(self, run: AgentRun) -> list[str] | None:
        found = self._step_runs.get(run.id)
        return found.criteria if found is not None else None

    # ---- context ---------------------------------------------------------------------------

    def requirements(self, project_id: uuid.UUID, task: PlanTask) -> list[dict[str, str]]:
        found = tools.get_requirements(self._tools, str(project_id), "confirmed")
        ordered = sorted(found["requirements"], key=lambda r: r["requirement_id"])
        out = []
        for n, item in enumerate(ordered, start=1):
            if item["requirement_id"] in task.requirement_ids:
                out.append(
                    {
                        "code": item["code"] or requirement_code(item["title"]) or f"R{n:03d}",
                        "type": item["type"],
                        "title": cut(item["title"], 200),
                        "description": cut(item["description"], 600),
                    }
                )
        return sorted(out, key=lambda r: r["code"])[:_MAX_BRIEF_REQUIREMENTS]

    def _brief(self, project_id: uuid.UUID, task: PlanTask, acknowledged: bool) -> dict[str, Any]:
        return {
            "key": task.key,
            "title": task.title,
            "kind": task.kind,
            "description": cut(task.description, 2000),
            "acceptance_criteria": [cut(c, 600) for c in task.acceptance_criteria[:20]],
            "flags": list(task.flags),
            "depends_on": list(task.depends_on),
            "unresolved_blocking_questions": (
                [cut(q, 600) for q in task.blocking_questions[:10]] if acknowledged else []
            ),
            "requirements": self.requirements(project_id, task),
        }

    # ---- graph -----------------------------------------------------------------------------

    def _graph(self, project_id: uuid.UUID, task: PlanTask) -> dict[str, Any]:
        return tools.graph_related(
            self._tools,
            str(project_id),
            task.source_ref,
            [str(r) for r in task.requirement_ids],
            per_root=_GRAPH_HITS_PER_ROOT,
        )

    def graph_context(self, project_id: uuid.UUID, task: PlanTask) -> dict[str, Any]:
        """The task's neighbours in the graph for the first brief; bounded, labelled as such."""
        found = self._graph(project_id, task)
        if not found["available"]:
            return {"note": f"Graph not available: {found['reason']}", "items": []}
        items = [
            {
                "node_id": r["node_id"],
                "kind": r["kind"],
                "label": cut(r["label"], 120),
                "affected_total": r["total"],
                "affected": [
                    {"kind": a["kind"], "label": cut(a["label"], 120), "via": a["via"]}
                    for a in r["affected"]
                ],
            }
            for r in found["roots"][:_GRAPH_ROOTS]
        ]
        return {
            "note": "Direct impact (get_impact, mode direct) of this task's item and of its "
            "linked requirements; the node_id can be passed to get_impact.",
            "items": items,
        }

    def graph_items(self, project_id: uuid.UUID, task: PlanTask) -> list[dict[str, str]]:
        """The task's graph items and the items they directly affect, unique and sorted."""
        found = self._graph(project_id, task)
        if not found["available"]:
            return []
        items: dict[str, dict[str, str]] = {}
        for root in found["roots"]:
            for node in [root, *root["affected"]]:
                items.setdefault(
                    str(node["node_id"]),
                    {"node_id": str(node["node_id"]), "kind": node["kind"], "label": node["label"]},
                )
        return sorted(items.values(), key=lambda i: (i["kind"], i["label"], i["node_id"]))

    def affected(
        self, project_id: uuid.UUID, plan: ImplementationPlan, task: PlanTask
    ) -> dict[str, Any]:
        """The requirements and tasks the change touches, according to the graph."""
        found = self._graph(project_id, task)
        if not found["available"]:
            return {"requirements": [], "tasks": [], "note": f"Graph: {found['reason']}"}
        by_ref = {
            t.source_ref: t for t in self._plans.tasks(project_id, plan.id) if t.key != task.key
        }
        requirements: dict[str, str] = {}
        tasks: dict[str, str] = {}
        for root in found["roots"]:
            for node in [root, *root["affected"]]:
                if node["kind"] == "requirement":
                    requirements.setdefault(node["source_ref"], node["label"])
                elif node["source_ref"] in by_ref:
                    other = by_ref[node["source_ref"]]
                    tasks.setdefault(other.key, other.title)
        return {
            "requirements": [
                {"id": rid, "title": cut(title, 200)} for rid, title in sorted(requirements.items())
            ],
            "tasks": [{"key": k, "title": cut(t, 200)} for k, t in sorted(tasks.items())],
        }

    # ---- actions ---------------------------------------------------------------------------

    # What the registry's agent tool handlers use of the service.
    @property
    def patches(self) -> PatchService:
        return self._patches

    @property
    def checker(self) -> StaticChecker:
        return self._checker

    @property
    def tool_context(self) -> tools.ToolContext:
        return self._tools

    def search_documents(self, project_id: uuid.UUID, query: str, top_k: int) -> dict[str, Any]:
        return tools.search_documents_auto(
            self._tools, str(project_id), query, top_k, self._embedder
        )

    def repository_context(self, project_id: uuid.UUID, task: PlanTask) -> dict[str, Any] | None:
        """The bounded code-map block of the connected repository, or None without one."""
        text = " ".join([task.title, task.description, *task.acceptance_criteria[:20]])
        return tools.repository_context(self._tools, str(project_id), text)

    def repository_files(self, project_id: uuid.UUID) -> dict[str, Any] | None:
        """Every file of the repository code map (checklist proposals), or None without one."""
        return tools.repository_files(self._tools, str(project_id))

    @property
    def limits(self) -> PatchLimits:
        return self._limits

    def write_problem(self, path: str, content: str) -> str | None:
        return write_problem(path, content, self._limits)

    def accepted_files(self, project_id: uuid.UUID) -> dict[str, str]:
        return {f.path: f.content for f in self._patch_repo.workspace(project_id)}

    def draft_failures(self, call: AgentCall) -> list[Failure]:
        """Why the draft is not a valid patch (the patch_validation hook)."""
        changes = call.draft.changes()
        current = self._patch_repo.workspace_hashes(call.project_id)
        base = call.draft.base_hashes()
        failures: list[Failure] = list(validate_patch(changes, current, self._limits))
        failures += stale_failures(
            [
                (c.path, c.change, None if c.change == "create" else base.get(c.path))
                for c in changes
            ],
            current,
        )
        if failures:
            self._log_failures(call.project_id, call.task, failures)
        return failures

    # ---- hooks -----------------------------------------------------------------------------

    def hook_states(self, project_id: uuid.UUID) -> list[dict[str, Any]]:
        """Every hook with whether it is enabled for the project."""
        disabled = self._agents.disabled_hooks(project_id)
        return [
            {
                "name": h.name,
                "point": h.point,
                "tools": sorted(h.tools) if h.tools else [],
                "order": h.order,
                "required": h.required,
                "description": h.description,
                "enabled": h.required or h.name not in disabled,
            }
            for h in get_hooks().all()
        ]

    def set_hook_enabled(self, project_id: uuid.UUID, name: str, enabled: bool) -> None:
        hook = get_hooks().get(name)
        if hook is None:
            raise HookNotFoundError(name)
        if hook.required and not enabled:
            raise HookRequiredError(name)
        self._agents.set_hook_enabled(project_id, name, enabled)

    def _agent_tools(self) -> dict[str, Tool]:
        return {t.name: t for t in get_registry().exposed("agent")}

    def offered_tools(self, *, ci: bool = False) -> list[Tool]:
        """The tools listed to the model: run_tests only with a real execution provider or in
        the CI repair loop (where it asks for a CI iteration)."""
        tests_ok, _ = self._tests.status()
        return [t for t in self._agent_tools().values() if ci or tests_ok or not t.needs_execution]

    def _act(
        self,
        action: Action,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        tasks: Any,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
        latency_ms: int,
        provider: str,
        model: str,
        based_on: int | None = None,
        *,
        hook_log: list[dict[str, str]] | None = None,
        disabled_hooks: frozenset[str] = frozenset(),
        check_cache: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        call = AgentCall(
            self, project_id, plan, task, draft, run, warnings, latency_ms, provider, model,
            based_on,
        )  # fmt: skip
        call.criteria = self._criteria(run)
        if check_cache is not None:
            call.check_cache = check_cache
        ci = self._ci.get(run.id)
        if ci is not None:
            ci.call = call
        tool = get_registry().get("agent", action.name)
        log = hook_log if hook_log is not None else []
        hooks = get_hooks()
        ctx = HookInput(call, action.name, action.args)
        points: list[Point] = ["before_action"]
        if action.name == "finish":
            points.append("before_finish")
        attach: dict[str, Any] = {}
        for point in points:
            outcome = hooks.run(point, action.name, ctx, disabled_hooks)
            log += outcome.log
            attach |= outcome.attach
            if outcome.blocked is not None:
                blocked = outcome.blocked
                raise ActionError(
                    blocked.code or "blocked",
                    blocked.reason,
                    path=cut(str(getattr(action.args, "path", "") or ""), 300) or None,
                    data=blocked.data,
                )
        result = tool.handler(call, action.args)
        if action.name != "finish":
            after = hooks.run("after_action", action.name, ctx, disabled_hooks)
            log += after.log
            attach |= after.attach
        return {**result, **attach}, call.done

    def run_tests(
        self,
        command_id: str,
        project_id: uuid.UUID,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Runs through the execution provider only; never locally. Unavailable is a result the
        model sees, and a note on the patch. In the CI repair loop it asks for a CI iteration."""
        ci = self._ci.get(run.id)
        if ci is not None:
            try:
                check_command(command_id)
            except UnknownCommandError:
                raise ActionError("unknown_command") from None
            validation = self._ci_validate(ci, draft)
            return {"summary": f"CI: {validation.status}", "ci": validation.feed}
        files = {p: f.content for p, f in draft.live().items()}
        baseline = {f.path: f.content for f in self._patch_repo.workspace(project_id)}
        # A job can wait minutes (CI approval, a workflow run): hold no write transaction meanwhile,
        # or a stop request could not be saved on SQLite.
        self._agents.commit()
        try:
            outcome = self._tests.run(
                run.id, command_id, files, baseline, [c.path for c in draft.changes()]
            )
        except UnknownCommandError:
            raise ActionError("unknown_command") from None
        except ProviderUnavailableError:
            note = "Tests were not run: tests unavailable (no execution provider is configured)."
            if note not in warnings:
                warnings.append(note)
            raise ActionError("tests_unavailable", data={"status": "unavailable"}) from None
        self._agents.add_jobs(
            [
                AgentJob(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    project_id=project_id,
                    command_id=j.command_id,
                    variant=j.variant,
                    select_test=j.select,
                    status=j.status,
                    timeout_seconds=j.timeout,
                    exit_code=j.exit_code,
                    duration_ms=j.duration_ms,
                    truncated=j.truncated,
                    summary={"job": j.id, **j.summary},
                    output=j.output,
                )
                for j in outcome.jobs
            ]
        )
        payload = outcome.payload
        flaky = payload.get("flaky") or []
        if flaky:
            warnings.append(
                "Flaky tests (failed, then passed on a lone re-run): " + ", ".join(flaky)
            )
        parts = [payload["status"]]
        if payload.get("structured"):
            parts += [
                f"{payload['new_in_changed']} new in changed files",
                f"{payload['new_elsewhere']} new elsewhere",
                f"{payload['pre_existing']} pre-existing",
            ]
        return {"summary": f"{command_id}: " + ", ".join(parts), **payload}

    def task_view(
        self, project_id: uuid.UUID, plan: ImplementationPlan, task: PlanTask, key: str | None
    ) -> dict[str, Any]:
        try:
            found = tools.get_plan(self._tools, str(project_id), plan.version, ["tasks"])
        except (tools.ToolInputError, PlanNotFoundError, ProjectNotFoundError):
            raise ActionError("task_not_found") from None
        wanted = key or task.key
        item = next((t for t in found["content"]["tasks"] if t.get("key") == wanted), None)
        if item is None:
            raise ActionError("task_not_found", path=None)
        return {"summary": f"task {wanted}", "task": item, "truncated": found["truncated"]}

    def finish_draft(
        self,
        a: Any,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
        latency_ms: int,
        provider: str,
        model: str,
        based_on: int | None = None,
        hook_notes: Sequence[str] = (),
    ) -> tuple[dict[str, Any], bool]:
        # The before_finish hooks have validated the draft and applied the coverage gate.
        notes = ([a.summary] if a.summary else []) + list(hook_notes) + list(a.notes)
        ci = self._ci.get(run.id)
        if ci is not None:
            if ci.loop.green_sig != draft_signature(draft):
                validation = self._ci_validate(ci, draft)
                if validation.terminal is not None:
                    return {"summary": f"CI: {validation.terminal}", "ci": validation.feed}, False
                if validation.status != "green":
                    raise ActionError(
                        "ci_not_green",
                        "Finish refused: CI is not green for this draft; fix the failures shown.",
                        data={"ci": validation.feed},
                    )
            notes = ci.loop.green_notes(warnings) + notes
        version = self._propose_draft(
            project_id, plan, task, draft, run, warnings, latency_ms, provider, model,
            notes, a.assumptions, a.test_suggestions, based_on,
        )  # fmt: skip
        run.summary = cut(a.summary, 1000) or None
        return {
            "summary": f"patch v{version} proposed",
            "patch_id": str(run.patch_id),
            "version": version,
        }, True

    def _stop_not_converging(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
        latency_ms: int,
        totals: list[int],
        findings: list[dict[str, Any]],
        based_on: int | None = None,
    ) -> None:
        """Hand the draft over for review with the last findings in the notes (if it is valid)."""
        counts = ", ".join(str(n) for n in totals[-10:])
        notes = [
            f"Not converging: the static finding count did not decrease for {CONVERGENCE_LIMIT} "
            f"consecutive checks (counts: {counts}). The findings below are still open."
        ] + [
            cut(f"{f['file']}:{f['line']} {f['tool']} {f['code']} {f['message']}", 300)
            for f in findings[:MAX_NOTE_FINDINGS]
        ]
        run.summary = f"Not converging after {len(totals)} static checks"
        try:
            self._propose_draft(
                project_id, plan, task, draft, run, warnings, latency_ms, run.provider, run.model,
                notes, [], [], based_on,
            )  # fmt: skip
        except ActionError as exc:
            logger.warning(
                "agent not-converging draft not proposed run=%s code=%s", run.id, exc.code
            )

    def _propose_draft(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
        latency_ms: int,
        provider: str,
        model: str,
        notes: list[str],
        assumptions: list[str],
        test_suggestions: list[str],
        based_on: int | None = None,
    ) -> int:
        changes = draft.changes()
        step_run = self._step_runs.get(run.id)
        if step_run is not None:
            notes = [step_run.note, *notes]
        try:
            detail = self._patches.propose(
                project_id,
                plan,
                task,
                GeneratedPatch(
                    files=changes,
                    notes=notes[:20],
                    assumptions=assumptions,
                    test_suggestions=test_suggestions,
                ),
                provider=provider,
                model=model,
                prompt_version=AGENT_PROMPT_VERSION,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                latency_ms=latency_ms,
                warnings=warnings,
                acknowledged=run.blocking_acknowledged,
                based_on_version=based_on,
                affected=self.affected(project_id, plan, task),
                criteria=step_run.criteria if step_run is not None else None,
            )
        except PatchInvalidError as exc:
            raise ActionError(
                "patch_invalid", data={"failures": [f.as_json() for f in exc.failures[:50]]}
            ) from None
        run.patch_id = detail.patch.id
        return detail.patch.version

    @staticmethod
    def _log_failures(project_id: uuid.UUID, task: PlanTask, failures: list[Failure]) -> None:
        logger.warning(
            "agent draft rejected project=%s task=%s codes=%s",
            project_id,
            task.key,
            sorted({f.code for f in failures}),
        )
