import logging
import threading
import time
import uuid
from collections.abc import Callable
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
    StepView,
    build_context,
    cut,
    dump,
    parse_action,
    write_problem,
)
from app.domain.architecture import safe_finish_reason
from app.domain.coverage import missing_findings, missing_lines
from app.domain.execution import (
    DEFAULT_OUTPUT_CAP,
    DEFAULT_TIMEOUT,
    ProviderUnavailableError,
    UnavailableExecutionProvider,
    UnknownCommandError,
)
from app.domain.patch import (
    Failure,
    GeneratedPatch,
    PatchInvalidError,
    PatchLimits,
    requirement_code,
    stale_failures,
    validate_patch,
)
from app.embeddings.contracts import EmbeddingProvider
from app.llm.agent_prompt import build_prompt, build_system_prompt
from app.llm.contracts import LLMProvider, LLMRequest
from app.mcp import tools
from app.models.agent import AgentJob, AgentRun, AgentStep
from app.models.plan import ImplementationPlan, PlanTask
from app.repositories.agent import AgentRepository
from app.repositories.patch import PatchRepository
from app.services.agent_tools import AgentCall
from app.services.execution import TestRunner, cancel_run_jobs, release_run
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


@dataclass(frozen=True)
class AgentConfig:
    max_steps: int
    token_budget: int
    time_limit_seconds: float
    step_max_output_tokens: int
    context_chars: int
    last_results: int


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
    ) -> None:
        self._embedder = embedder
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
        return list(self._agents.jobs(project_id, run_id))

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
    ) -> AgentRun:
        plan, task, _ = self._patches.check_gates(
            project_id, plan_id, task_key, acknowledge_blocking_questions
        )
        if not _agent_lock.acquire(blocking=False):
            raise PatchBusyError("An agent run is already in progress")
        try:
            run = AgentRun(
                id=uuid.uuid4(),
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
                model=self._provider.model_name,
                prompt_version=AGENT_PROMPT_VERSION,
            )
            run = self._agents.add_run(run, self._patch_repo.workspace(project_id))
        except Exception:
            _agent_lock.release()
            raise
        _active.add(run.id)
        return run

    # ---- loop ------------------------------------------------------------------------------

    def execute(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Run the loop of a started run to its end. Never raises; the lock is always released."""
        try:
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
        self._agents.drop_draft(run.project_id, run.id)
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
        decision_block, brief["flags"] = self._patches.decision_context(project_id, plan, task)
        feedback, based_on = self._patches.feedback_context(project_id, task)
        framework_rules = self._patches.frameworks(project_id, draft.live()).context()
        if framework_rules:
            brief["framework_rules"] = framework_rules
        tests_ok, _ = self._tests.status()
        if tests_ok:
            brief["run_tests"] = "available: pytest, ruff, mypy, tsc, npm test"
        offered = self.offered_tools()
        system_prompt = build_system_prompt(offered)
        tool_models = {t.name: t.args_model for t in self._agent_tools().values()}
        steps: list[StepView] = []
        started = self._clock()
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
            except Exception as exc:
                logger.error("agent provider error run=%s error=%s", run_id, type(exc).__name__)
                self._end(run, "failed", "provider_error", self._clock() - started)
                return
            used_in = response.usage.input_tokens or 0
            used_out = response.usage.output_tokens or 0
            run.input_tokens += used_in
            run.output_tokens += used_out
            total_latency += response.latency_ms
            run.provider, run.model = response.provider, response.model
            action_name, path, args = "invalid", None, {}
            result: dict[str, Any]
            error: ActionError | None = None
            done = False
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
                )
            except ActionError as exc:
                error = exc
                path = path or (cut(exc.path, 300) if exc.path else None)
                result = {"error": exc.code, "message": str(exc), **exc.data}
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
                self._end(run, "finished", "finished", self._clock() - started, run.summary)
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
            if action_name == "run_static_checks" and error is None and result.get("total"):
                total = int(result["total"])
                check_totals.append(total)
                last_findings = list(result.get("findings") or [])
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
            elif action_name == "run_static_checks" and error is None:
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

    def write_problem(self, path: str, content: str) -> str | None:
        return write_problem(path, content, self._limits)

    def _agent_tools(self) -> dict[str, Tool]:
        return {t.name: t for t in get_registry().exposed("agent")}

    def offered_tools(self) -> list[Tool]:
        """The tools listed to the model: run_tests only with a real execution provider."""
        tests_ok, _ = self._tests.status()
        return [t for t in self._agent_tools().values() if tests_ok or not t.needs_execution]

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
    ) -> tuple[dict[str, Any], bool]:
        call = AgentCall(
            self, project_id, plan, task, draft, run, warnings, latency_ms, provider, model,
            based_on,
        )  # fmt: skip
        tool = get_registry().get("agent", action.name)
        return tool.handler(call, action.args), call.done

    def run_tests(
        self,
        command_id: str,
        project_id: uuid.UUID,
        draft: Draft,
        run: AgentRun,
        warnings: list[str],
    ) -> dict[str, Any]:
        """Runs through the execution provider only; never locally. Unavailable is a result the
        model sees, and a note on the patch."""
        files = {p: f.content for p, f in draft.live().items()}
        baseline = {f.path: f.content for f in self._patch_repo.workspace(project_id)}
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
    ) -> tuple[dict[str, Any], bool]:
        changes = draft.changes()
        current = self._patch_repo.workspace_hashes(project_id)
        base = draft.base_hashes()
        failures: list[Failure] = list(validate_patch(changes, current, self._limits))
        failures += stale_failures(
            [
                (c.path, c.change, None if c.change == "create" else base.get(c.path))
                for c in changes
            ],
            current,
        )
        if failures:
            self._log_failures(project_id, task, failures)
            raise ActionError(
                "patch_invalid",
                data={"failures": [f.as_json() for f in failures[:50]]},
            )
        coverage = self._patches.coverage(
            project_id, task, {p: f.content for p, f in draft.live().items()}
        )
        open_items = missing_lines(coverage)
        coverage_notes: list[str] = []
        if open_items:
            if run.steps_taken + 1 < run.max_steps:
                raise ActionError(
                    "acceptance_missing",
                    f"Finish refused: {len(open_items)} acceptance item(s) name terms the draft "
                    "does not define. Add them to the code (names, roles, statuses) and finish "
                    "again.",
                    data={
                        "findings": missing_findings(coverage),
                        "coverage": {k: coverage[k] for k in ("covered", "missing", "unchecked")},
                    },
                )
            coverage_notes = [f"Not covered at the step limit: {line}" for line in open_items]
        notes = ([a.summary] if a.summary else []) + coverage_notes + list(a.notes)
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
