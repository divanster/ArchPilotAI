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
    MAX_READ_CHARS,
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
    search_draft,
    write_problem,
)
from app.domain.architecture import safe_finish_reason
from app.domain.patch import (
    Failure,
    GeneratedPatch,
    PatchInvalidError,
    PatchLimits,
    path_problem,
    requirement_code,
    stale_failures,
    validate_patch,
)
from app.llm.agent_prompt import SYSTEM_PROMPT, build_prompt
from app.llm.contracts import LLMProvider, LLMRequest
from app.mcp import tools
from app.models.agent import AgentRun, AgentStep
from app.models.plan import ImplementationPlan, PlanTask
from app.repositories.agent import AgentRepository
from app.repositories.patch import PatchRepository
from app.services.patch import PatchBusyError, PatchService, TaskNotFoundError
from app.services.plan import PlanNotFoundError
from app.services.project import ProjectNotFoundError
from app.services.static_checks import StaticChecker

logger = logging.getLogger(__name__)

# One agent run at a time per process: bounds provider cost and temporary-directory use.
_agent_lock = threading.Lock()
_active: set[uuid.UUID] = set()
_MAX_BRIEF_REQUIREMENTS = 20
_SIZE_ARGS = frozenset({"content", "search", "replace"})


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


def _log_args(action: Action) -> dict[str, Any]:
    """Step arguments for the log: sizes instead of contents, short strings only."""
    out: dict[str, Any] = {}
    for key, value in action.args.model_dump().items():
        if key in _SIZE_ARGS:
            out[f"{key}_chars"] = len(value)
        elif isinstance(value, str):
            out[key] = cut(value, 200)
        elif isinstance(value, list):
            out[key] = len(value)
        else:
            out[key] = value
    return out


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
    ) -> None:
        self._provider = provider
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
        steps: list[StepView] = []
        started = self._clock()
        total_latency = 0
        previous: tuple[str, str | None, str | None] | None = None
        repeats = 0
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
                        system_prompt=SYSTEM_PROMPT,
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
                )
                action_name = action.name
                path = cut(str(getattr(action.args, "path", "") or ""), 300) or None
                args = _log_args(action)
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
            if repeats >= REPEAT_LIMIT:
                self._end(run, "stopped", "repeated_error", self._clock() - started)
                return

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

    def _requirements(self, project_id: uuid.UUID, task: PlanTask) -> list[dict[str, str]]:
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
            "requirements": self._requirements(project_id, task),
        }

    # ---- actions ---------------------------------------------------------------------------

    @staticmethod
    def _existing(draft: Draft, path: str) -> DraftFile:
        problem = path_problem(path)
        if problem:
            raise ActionError(problem, f"Refused: {problem}", path=path)
        file = draft.get(path)
        if file is None:
            raise ActionError("file_not_found", path=path)
        return file

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
    ) -> tuple[dict[str, Any], bool]:
        a = action.args
        name = action.name
        if name == "read_file":
            file = self._existing(draft, a.path)
            lines = file.content.splitlines()
            start = a.start_line or 1
            end = min(a.end_line or len(lines), len(lines))
            text = "\n".join(lines[start - 1 : end]) if lines else ""
            body = cut(text, MAX_READ_CHARS)
            return {
                "summary": f"read {max(end - start + 1, 0)} line(s)",
                "path": a.path,
                "content": body,
                "start_line": start,
                "end_line": end,
                "total_lines": len(lines),
                "truncated": len(body) < len(text),
            }, False
        if name == "list_files":
            paths = sorted(p for p in draft.live() if p.startswith(a.prefix))
            shown = paths[: a.limit]
            return {
                "summary": f"{len(shown)} of {len(paths)} file(s)",
                "files": [
                    {"path": p, "bytes": len(draft.files[p].content.encode())} for p in shown
                ],
                "total": len(paths),
                "truncated": len(paths) > len(shown),
            }, False
        if name == "search_code":
            matches, total, truncated = search_draft(draft, a.query, a.prefix)
            return {
                "summary": f"{total} match(es)",
                "matches": matches,
                "total": total,
                "truncated": truncated,
            }, False
        if name == "get_requirements":
            reqs = self._requirements(project_id, task)
            return {"summary": f"{len(reqs)} requirement(s)", "requirements": reqs}, False
        if name == "get_task":
            return self._get_task(project_id, plan, task, a.key), False
        if name == "get_impact":
            try:
                found = tools.get_impact(self._tools, str(project_id), a.node_id, a.mode)
            except tools.ToolInputError as exc:
                raise ActionError("tool_input", str(exc)) from None
            return {"summary": f"{found['total']} impacted node(s)", **found}, False
        if name == "edit_file":
            file = self._existing(draft, a.path)
            count = file.content.count(a.search)
            if count == 0:
                raise ActionError("no_match", path=a.path)
            if count > 1:
                raise ActionError("multiple_matches", path=a.path, data={"matches": count})
            new = file.content.replace(a.search, a.replace, 1)
            if new == file.content:
                raise ActionError("unchanged", path=a.path)
            problem = write_problem(a.path, new, self._limits)
            if problem:
                raise ActionError(problem, f"Refused: {problem}", path=a.path)
            draft.put(a.path, new)
            return {
                "summary": "edited 1 occurrence",
                "path": a.path,
                "bytes": len(new.encode()),
            }, False
        if name == "write_file":
            problem = write_problem(a.path, a.content, self._limits)
            if problem:
                raise ActionError(problem, f"Refused: {problem}", path=a.path)
            if draft.clash(a.path) is not None:
                raise ActionError("file_exists", path=a.path)
            draft.put(a.path, a.content)
            return {
                "summary": f"wrote {len(a.content.encode())} byte(s)",
                "path": a.path,
                "bytes": len(a.content.encode()),
            }, False
        if name == "delete_file":
            self._existing(draft, a.path)
            draft.delete(a.path)
            return {"summary": "deleted", "path": a.path}, False
        if name == "run_static_checks":
            report = self._checker.run({p: f.content for p, f in draft.live().items()})
            data = report.as_json()
            return {"summary": f"{report.total} finding(s)", **data}, False
        return self._finish(
            a, project_id, plan, task, draft, run, warnings, latency_ms, provider, model
        )

    def _get_task(
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

    def _finish(
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
        notes = ([a.summary] if a.summary else []) + list(a.notes)
        try:
            detail = self._patches.propose(
                project_id,
                plan,
                task,
                GeneratedPatch(
                    files=changes,
                    notes=notes[:20],
                    assumptions=a.assumptions,
                    test_suggestions=a.test_suggestions,
                ),
                provider=provider,
                model=model,
                prompt_version=AGENT_PROMPT_VERSION,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                latency_ms=latency_ms,
                warnings=warnings,
                acknowledged=run.blocking_acknowledged,
            )
        except PatchInvalidError as exc:
            raise ActionError(
                "patch_invalid", data={"failures": [f.as_json() for f in exc.failures[:50]]}
            ) from None
        run.patch_id = detail.patch.id
        run.summary = cut(a.summary, 1000) or None
        return {
            "summary": f"patch v{detail.patch.version} proposed",
            "patch_id": str(detail.patch.id),
            "version": detail.patch.version,
        }, True

    @staticmethod
    def _log_failures(project_id: uuid.UUID, task: PlanTask, failures: list[Failure]) -> None:
        logger.warning(
            "agent draft rejected project=%s task=%s codes=%s",
            project_id,
            task.key,
            sorted({f.code for f in failures}),
        )
