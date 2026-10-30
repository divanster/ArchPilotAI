"""Task decomposition before coding (Phase 13D).

A plan task gets a proposed, ordered checklist of coding steps. A person approves it before the
first coding run; the agent may propose revisions but never approves. Approved checklists are
read-only, and the agent codes one step per run, starting from the files accepted for earlier
steps. A failed or stopped step run keeps its draft as a checkpoint that a new run resumes from.
"""

import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.checklist import (
    Candidates,
    ChecklistInvalidError,
    Decomposition,
    Problem,
    RelatedItem,
    Step,
    build_steps,
    known_files,
    validate_checklist,
)
from app.llm.checklist_prompt import (
    SYSTEM_PROMPT,
    ChecklistAnswerError,
    build_prompt,
    parse_answer,
)
from app.llm.contracts import LLMProvider, LLMRequest
from app.llm.exceptions import LLMError
from app.models.agent import AgentRun, AgentStep
from app.models.checklist import TaskChecklist
from app.models.patch import CodePatch
from app.models.plan import ImplementationPlan, PlanTask
from app.repositories.agent import AgentRepository
from app.repositories.checklist import ChecklistRepository
from app.repositories.patch import PatchRepository
from app.services.agent import AgentService, is_active
from app.services.patch import PatchGateError

# Names that stand for software, never for the person who approves or rejects.
RESERVED_NAMES = frozenset({"agent", "system", "ai", "assistant", "model", "llm", "bot"})
CHECKLIST_MAX_OUTPUT_TOKENS = 4000
KNOWN_FILES_IN_PROMPT = 300


class ChecklistNotFoundError(Exception):
    def __init__(self, task_key: str) -> None:
        super().__init__(f"{task_key} has no checklist yet")


class ChecklistReadOnlyError(Exception):
    """The checklist is approved: it can no longer change (HTTP 409)."""


class ChecklistStateError(Exception):
    """The checklist or step is not in a state that allows this action (HTTP 409)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DeciderError(Exception):
    """The approver or rejecter is missing or is not a person (HTTP 422)."""


class AgentRevisionError(Exception):
    """The model's revision could not be read (HTTP 502)."""


@dataclass(frozen=True)
class StepState:
    step: Step
    status: str  # todo | running | proposed | accepted | rejected | failed | stopped
    run: AgentRun | None
    patch: CodePatch | None
    resumable: bool


@dataclass(frozen=True)
class ChecklistView:
    task_key: str
    checklist: TaskChecklist | None
    steps: list[StepState]
    next_step: str | None
    history: list[TaskChecklist]


def _decider(name: str, reason: str) -> tuple[str, str]:
    name = " ".join(name.split())
    reason = reason.strip()
    if not name or not reason:
        raise DeciderError("A name and a reason are required")
    if name.casefold() in RESERVED_NAMES:
        raise DeciderError("Only a person can approve or reject a checklist")
    return name[:100], reason[:1000]


class ChecklistService:
    def __init__(
        self,
        provider: LLMProvider,
        agent: AgentService,
        checklists: ChecklistRepository,
        agents: AgentRepository,
        patches: PatchRepository,
    ) -> None:
        self._provider = provider
        self._agent = agent
        self._patches = agent.patches
        self._checklists = checklists
        self._agents = agents
        self._patch_repo = patches

    @property
    def agent(self) -> AgentService:
        return self._agent

    def run_steps(self, run: AgentRun) -> list[AgentStep]:
        return list(self._agents.steps(run.project_id, run.id))

    # ---- read ------------------------------------------------------------------------------

    def _task(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> tuple[ImplementationPlan, PlanTask, Sequence[PlanTask]]:
        return self._patches.plan_and_task(project_id, plan_id, task_key)

    def view(self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str) -> ChecklistView:
        _, task, _ = self._task(project_id, plan_id, task_key)
        current = self._checklists.current(project_id, task.id)
        history = list(self._checklists.history(project_id, task.id))
        if current is None:
            return ChecklistView(task.key, None, [], None, history)
        states = self.states(project_id, current)
        return ChecklistView(task.key, current, states, self._next(current, states), history)

    def states(self, project_id: uuid.UUID, checklist: TaskChecklist) -> list[StepState]:
        steps = [Step.from_json(s) for s in checklist.steps]
        runs = list(self._agents.runs_for_checklist(project_id, checklist.id))
        for run in runs:
            if run.status == "running" and not is_active(run.id):
                self._agent.mark_interrupted(project_id, run.id)
        patches: dict[uuid.UUID, CodePatch] = {}
        for run in runs:
            if run.patch_id is not None:
                found = self._patch_repo.get(project_id, run.patch_id)
                if found is not None:
                    patches[run.patch_id] = found
        with_draft = self._agents.runs_with_draft(
            project_id, [r.id for r in runs if r.status in ("failed", "stopped")]
        )
        out: list[StepState] = []
        for step in steps:
            mine = [r for r in runs if r.checklist_step_key == step.key]
            accepted = next(
                (
                    r
                    for r in mine
                    if r.patch_id is not None
                    and r.patch_id in patches
                    and patches[r.patch_id].status == "accepted"
                ),
                None,
            )
            latest = accepted or (mine[0] if mine else None)
            patch = patches.get(latest.patch_id) if latest is not None and latest.patch_id else None
            if latest is None:
                status = "todo"
            elif latest.status == "running":
                status = "running"
            elif patch is not None:
                status = patch.status
            elif latest.status in ("failed", "stopped"):
                status = latest.status
            else:
                status = "failed"  # finished without a patch
            resumable = (
                latest is not None
                and patch is None
                and latest.status in ("failed", "stopped")
                and latest.id in with_draft
            )
            out.append(StepState(step, status, latest, patch, resumable))
        return out

    @staticmethod
    def _next(checklist: TaskChecklist, states: Sequence[StepState]) -> str | None:
        if checklist.status != "approved":
            return None
        return next((s.step.key for s in states if s.status != "accepted"), None)

    # ---- propose ---------------------------------------------------------------------------

    def context(
        self, project_id: uuid.UUID, task: PlanTask, tasks: Sequence[PlanTask]
    ) -> tuple[Candidates, dict[str, str]]:
        """What a step may point at, and every existing file a revision may name (path -> source).

        Accepted files of the task and its dependencies, the repository code map and the task's
        graph items. Without a repository, steps leave file discovery to the run.
        """
        dependencies = [t for t in tasks if t.key in set(task.depends_on)]
        repo = self._agent.repository_files(project_id)
        state = "none" if repo is None else "unavailable"
        files: dict[str, list[str]] = {}
        other: list[str] = []
        roots: list[str] = []
        if repo is not None and repo.get("available"):
            state = "mapped"
            files = {str(p): [str(s) for s in syms] for p, syms in repo["files"].items()}
            other = [str(p) for p in repo.get("other_paths", [])]
            roots = [str(r) for r in repo.get("source_roots", [])]
        candidates = Candidates(
            task_files=sorted(self._patches.accepted_files(project_id, [task])),
            dependency_files=self._patches.accepted_files(project_id, dependencies),
            repository_files=files,
            graph_items=[
                RelatedItem(i["node_id"], i["kind"], i["label"])
                for i in self._agent.graph_items(project_id, task)
            ],
            repository=state,
            source_roots=roots,
        )
        known = known_files(candidates)
        for path in other:
            known.setdefault(path, "repository")
        return candidates, known

    def candidates(
        self, project_id: uuid.UUID, task: PlanTask, tasks: Sequence[PlanTask]
    ) -> Candidates:
        return self.context(project_id, task, tasks)[0]

    def _writable(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> tuple[ImplementationPlan, PlanTask, Sequence[PlanTask], TaskChecklist | None]:
        plan, task, tasks = self._task(project_id, plan_id, task_key)
        if plan.status != "approved":
            raise PatchGateError(
                "plan_not_approved",
                f"The plan is {plan.status}; only tasks of an approved plan get a checklist",
            )
        current = self._checklists.current(project_id, task.id)
        if current is not None and current.status == "approved":
            raise ChecklistReadOnlyError(f"The checklist of {task.key} is approved and read-only")
        return plan, task, tasks, current

    def _save(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        result: Decomposition,
        warnings: Sequence[str],
        by: str,
        reason: str | None,
        previous: TaskChecklist | None,
    ) -> TaskChecklist:
        return self._checklists.add(
            TaskChecklist(
                id=uuid.uuid4(),
                project_id=project_id,
                plan_id=plan.id,
                task_id=task.id,
                task_key=task.key,
                version=0,
                status="proposed",
                steps=[s.as_json() for s in result.steps],
                warnings=list(warnings),
                manual_verification=[m.as_json() for m in result.manual],
                proposed_by=by,
                proposal_reason=reason,
            ),
            supersede=previous,
        )

    def build(self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str) -> TaskChecklist:
        """The deterministic proposal from the task's criteria; no model involved."""
        plan, task, tasks, current = self._writable(project_id, plan_id, task_key)
        result = build_steps(
            task.key,
            task.title,
            list(task.acceptance_criteria),
            self.candidates(project_id, task, tasks),
        )
        return self._save(
            project_id,
            plan,
            task,
            result,
            result.warnings,
            "system",
            "Built from the task",
            current,
        )

    def revise(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_key: str,
        author: str,
        reason: str,
        raw: Sequence[Mapping[str, Any]],
        manual: Sequence[Mapping[str, Any]] = (),
    ) -> TaskChecklist:
        """A person's revision: validated, saved as a new proposed version."""
        name, why = _decider(author, reason)
        plan, task, tasks, current = self._writable(project_id, plan_id, task_key)
        if current is None:
            raise ChecklistNotFoundError(task.key)
        previous = [Step.from_json(s) for s in current.steps]
        _, known = self.context(project_id, task, tasks)
        result = validate_checklist(raw, manual, task.acceptance_criteria, previous, known)
        return self._save(project_id, plan, task, result, current.warnings, name, why, current)

    def agent_revise(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, instruction: str
    ) -> TaskChecklist:
        """The model proposes a revision. It is validated like a person's and stays proposed."""
        plan, task, tasks, current = self._writable(project_id, plan_id, task_key)
        if current is None:
            raise ChecklistNotFoundError(task.key)
        previous = [Step.from_json(s) for s in current.steps]
        candidates, known = self.context(project_id, task, tasks)
        context = {
            "task": {
                "key": task.key,
                "title": task.title,
                "acceptance_criteria": list(task.acceptance_criteria),
            },
            "steps": [
                {k: v for k, v in s.as_json().items() if k != "related_items"} for s in previous
            ],
            "manual_verification": list(current.manual_verification),
            "repository": candidates.repository,
            "known_files": sorted(known)[:KNOWN_FILES_IN_PROMPT],
            "instruction": instruction.strip()[:2000],
        }
        try:
            response = self._provider.generate(
                LLMRequest(
                    prompt=build_prompt(context, secrets.token_hex(8)),
                    system_prompt=SYSTEM_PROMPT,
                    max_output_tokens=CHECKLIST_MAX_OUTPUT_TOKENS,
                    temperature=0,
                )
            )
            raw, manual, reason = parse_answer(response.text)
        except (LLMError, ChecklistAnswerError) as exc:
            raise AgentRevisionError(f"The agent revision failed: {type(exc).__name__}") from exc
        result = validate_checklist(raw, manual, task.acceptance_criteria, previous, known)
        return self._save(
            project_id,
            plan,
            task,
            result,
            current.warnings,
            "agent",
            reason or "Agent revision",
            current,
        )

    # ---- decide ----------------------------------------------------------------------------

    def _proposed(self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str) -> TaskChecklist:
        _, task, _ = self._task(project_id, plan_id, task_key)
        current = self._checklists.current(project_id, task.id)
        if current is None:
            raise ChecklistNotFoundError(task.key)
        if current.status == "approved":
            raise ChecklistReadOnlyError(f"The checklist of {task.key} is approved and read-only")
        if current.status != "proposed":
            raise ChecklistStateError(
                "checklist_not_proposed",
                f"The checklist of {task.key} is {current.status}; propose a new version first",
            )
        return current

    def approve(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, by: str, reason: str
    ) -> TaskChecklist:
        name, why = _decider(by, reason)
        current = self._proposed(project_id, plan_id, task_key)
        _, task, _ = self._task(project_id, plan_id, task_key)
        # A stored checklist is re-checked: the task criteria may have changed since. Paths were
        # checked when the version was proposed; the repository may have moved on since.
        try:
            validate_checklist(current.steps, current.manual_verification, task.acceptance_criteria)
        except ChecklistInvalidError as exc:
            raise ChecklistInvalidError(
                [
                    Problem("task_changed", "The task changed since this checklist was proposed"),
                    *exc.problems,
                ]
            ) from exc
        legacy = next(
            (
                r
                for r in self._agents.runs_for_task(project_id, task.id)
                if r.checklist_id is None and r.status == "running" and is_active(r.id)
            ),
            None,
        )
        if legacy is not None:
            raise ChecklistStateError(
                "legacy_run_active",
                f"A task-level agent run of {task.key} is still running; wait for it or stop it "
                "before switching the task to step-based mode",
            )
        return self._checklists.decide(current, "approved", name, why)

    def reject(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, by: str, reason: str
    ) -> TaskChecklist:
        name, why = _decider(by, reason)
        current = self._proposed(project_id, plan_id, task_key)
        return self._checklists.decide(current, "rejected", name, why)

    # ---- run -------------------------------------------------------------------------------

    def _approved(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> tuple[TaskChecklist, list[StepState], StepState]:
        _, task, _ = self._task(project_id, plan_id, task_key)
        current = self._checklists.current(project_id, task.id)
        if current is None:
            raise ChecklistNotFoundError(task.key)
        if current.status != "approved":
            raise ChecklistStateError(
                "checklist_not_approved",
                f"The checklist of {task.key} is {current.status}; a person must approve it "
                "before the first coding run",
            )
        states = self.states(project_id, current)
        busy = next((s for s in states if s.status in ("running", "proposed")), None)
        if busy is not None:
            raise ChecklistStateError(
                "step_in_progress",
                f"Step {busy.step.key} is {busy.status}; finish its review before the next step",
            )
        key = self._next(current, states)
        if key is None:
            raise ChecklistStateError("checklist_complete", "Every step is accepted")
        return current, states, next(s for s in states if s.step.key == key)

    def run_next(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_key: str,
        *,
        acknowledge_blocking_questions: bool = False,
        restart: bool = False,
        max_steps: int | None = None,
        deployment: str | None = None,
    ) -> AgentRun:
        """Start a run of the next step from the accepted workspace."""
        current, _, state = self._approved(project_id, plan_id, task_key)
        if state.resumable and not restart:
            raise ChecklistStateError(
                "resume_available",
                f"Step {state.step.key} has a saved checkpoint; resume it or restart the step",
            )
        return self._agent.start_run(
            project_id,
            plan_id,
            task_key,
            acknowledge_blocking_questions=acknowledge_blocking_questions,
            max_steps=max_steps,
            deployment=deployment,
            checklist_id=current.id,
            step_key=state.step.key,
            discard_checkpoint=state.run.id if state.resumable and state.run else None,
        )

    def resume(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_key: str,
        step_key: str,
        *,
        acknowledge_blocking_questions: bool = False,
        max_steps: int | None = None,
        deployment: str | None = None,
    ) -> AgentRun:
        """Continue a failed or stopped step from its last draft checkpoint."""
        current, states, state = self._approved(project_id, plan_id, task_key)
        if all(s.step.key != step_key for s in states):
            raise ChecklistStateError(
                "unknown_step", f"Step {step_key[:20]} is not in the checklist"
            )
        if state.step.key != step_key:
            raise ChecklistStateError(
                "not_next_step", f"Only the next step ({state.step.key}) can run"
            )
        if not state.resumable or state.run is None:
            raise ChecklistStateError(
                "no_checkpoint", f"Step {step_key} has no saved checkpoint to resume from"
            )
        return self._agent.start_run(
            project_id,
            plan_id,
            task_key,
            acknowledge_blocking_questions=acknowledge_blocking_questions,
            max_steps=max_steps,
            deployment=deployment,
            checklist_id=current.id,
            step_key=step_key,
            resume_from=state.run.id,
        )
