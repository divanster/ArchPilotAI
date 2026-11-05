"""The TUI's only door to ArchPilotAI: the existing services, on the same database.

No business logic and no state of its own lives here. Each call opens a session, builds the
services exactly as the HTTP layer does (app.api.deps and the routers' factories), calls one
service function and returns plain data. Agent runs are launched with the HTTP layer's own
launcher, so they run in the same background worker the web UI uses.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker

from app.api import agent as agent_api
from app.api import checklists as checklists_api
from app.api import deps
from app.api import git_remote as git_api
from app.domain.git_remote import GitSettingsError
from app.llm.contracts import LLMProvider
from app.llm.exceptions import LLMConfigurationError
from app.services.agent import (
    CheckpointStaleError,
    HookRequiredError,
    RoutingPolicyError,
    RunNotFoundError,
    RunNotRunningError,
    is_active,
)
from app.services.change_request import (
    ChangeRequestInputError,
    ChangeRequestNotFoundError,
    ChangeRequestService,
    ChangeRequestStateError,
    NoBaselineError,
    effective_class,
)
from app.services.checklist import (
    AgentRevisionError,
    ChecklistReadOnlyError,
    ChecklistService,
    ChecklistStateError,
    DeciderError,
)
from app.services.ci_loop import CILoopError, CIResumeRefusedError
from app.services.credential_store import CredentialStoreUnavailableError
from app.services.git_remote import GitRemoteError, SettingsNotFoundError
from app.services.patch import (
    PatchBusyError,
    PatchGateError,
    PatchNotFoundError,
    PatchNotProposedError,
    RemovalsNotConfirmedError,
    ReviewInputError,
    StalePatchError,
    TaskNotFoundError,
)
from app.services.plan import PlanNotFoundError
from app.services.project import ProjectNotFoundError
from app.services.repo_map import read_head

# Service errors whose messages are written for people and never carry secrets.
_SAFE_ERRORS: tuple[type[Exception], ...] = (
    ProjectNotFoundError,
    PlanNotFoundError,
    TaskNotFoundError,
    RunNotFoundError,
    RunNotRunningError,
    RoutingPolicyError,
    CheckpointStaleError,
    HookRequiredError,
    AgentRevisionError,
    ChecklistReadOnlyError,
    PatchGateError,
    PatchBusyError,
    PatchNotFoundError,
    PatchNotProposedError,
    StalePatchError,
    RemovalsNotConfirmedError,
    ReviewInputError,
    ChangeRequestInputError,
    ChangeRequestNotFoundError,
    ChangeRequestStateError,
    NoBaselineError,
    ChecklistStateError,
    DeciderError,
    CILoopError,
    CIResumeRefusedError,
    GitRemoteError,
    GitSettingsError,
    CredentialStoreUnavailableError,
)


class TuiError(Exception):
    """A refusal or failure, with a message safe to show on screen."""


@contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except TuiError:
        raise
    except HTTPException as exc:
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("message", "")
        raise TuiError(str(detail)) from None
    except LLMConfigurationError as exc:
        raise TuiError(exc.public_message) from None
    except _SAFE_ERRORS as exc:
        raise TuiError(str(exc)) from None
    except Exception as exc:
        # The type only: other messages may carry draft content or connection details.
        raise TuiError(f"Unexpected error ({type(exc).__name__})") from None


# ---- plain views -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeView:
    key: str
    impact_class: str
    reason: str
    components: list[str]


@dataclass(frozen=True)
class ProjectRow:
    id: uuid.UUID
    name: str
    status: str


@dataclass(frozen=True)
class RepositoryView:
    provider: str
    remote_url: str
    clone_folder: str
    base_branch: str
    branch_prefix: str
    cloned: bool
    current_branch: str | None
    pat: str  # "none" or "ends with ...abcd"; never the token


@dataclass(frozen=True)
class MissionView:
    project: ProjectRow
    architecture: str
    plan: str
    plan_id: uuid.UUID | None
    repository: RepositoryView | None


@dataclass(frozen=True)
class TaskRow:
    key: str
    title: str
    kind: str
    status: str
    depends_on: list[str]
    blocking_questions: int


@dataclass(frozen=True)
class StepRow:
    key: str
    title: str
    goal: str
    depends_on: list[str]
    status: str
    resumable: bool
    run_id: uuid.UUID | None
    patch_id: uuid.UUID | None


@dataclass(frozen=True)
class ChecklistInfo:
    task_key: str
    version: int | None
    status: str  # none | proposed | approved | rejected
    next_step: str | None
    steps: list[StepRow]


@dataclass(frozen=True)
class PatchFileView:
    path: str
    change: str
    diff: str
    truncated: bool


@dataclass(frozen=True)
class PatchView:
    id: uuid.UUID
    task_key: str
    version: int
    status: str
    files: list[PatchFileView]
    coverage: dict[str, int]
    missing: list[str]
    removals: list[tuple[str, str]]  # (file, name) removed from accepted files
    stale: list[str]
    reviewed_by: str | None
    review_reason: str | None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Target:
    """What an approval-tier action touches, shown on the confirmation screen."""

    action: str
    repository: str
    remote: str
    branch: str


def _project_row(project: Any) -> ProjectRow:
    return ProjectRow(
        project.id, project.name, str(getattr(project.status, "value", project.status))
    )


def pat_label(last4: str | None) -> str:
    return f"ends with ...{last4}" if last4 else "none"


# ---- the facade --------------------------------------------------------------------------------


class Backend:
    def __init__(
        self,
        factory: sessionmaker[Session],
        provider: Callable[[], LLMProvider] = deps.get_llm_provider,
    ) -> None:
        self._factory = factory
        self._provider = provider

    @classmethod
    def from_settings(cls) -> "Backend":
        return cls(deps.get_session_factory())

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with _guard(), self._factory() as session:
            yield session

    # ---- projects and mission control ------------------------------------------------------

    def projects(self) -> list[ProjectRow]:
        with self._session() as s:
            return [
                ProjectRow(
                    p.id, p.name, str(p.status.value if hasattr(p.status, "value") else p.status)
                )
                for p in deps.get_project_service(s).list()
            ]

    def find_project(self, ref: str) -> ProjectRow:
        """A project by id or (case-insensitive) exact name."""
        rows = self.projects()
        try:
            wanted = uuid.UUID(ref)
            found = [r for r in rows if r.id == wanted]
        except ValueError:
            found = [r for r in rows if r.name.casefold() == ref.strip().casefold()]
        if not found:
            raise TuiError(f"No project matches {ref!r}")
        if len(found) > 1:
            raise TuiError(f"More than one project is named {ref!r}; use its id")
        return found[0]

    def mission(self, project_id: uuid.UUID) -> MissionView:
        with self._session() as s:
            project = deps.get_project_service(s).get(project_id)
            row = _project_row(project)
            proposals = deps.get_architecture_service(s).list(project_id)
            if proposals:
                newest = proposals[0]
                approved = [p for p in proposals if p.status == "approved"]
                architecture = f"v{newest.version} {newest.status}"
                if approved and approved[0] is not newest:
                    architecture += f" (approved: v{approved[0].version})"
            else:
                architecture = "none"
            plans = deps.get_plan_service(s)
            listed, _ = plans.list(project_id)
            approved_plan = plans.get_version(project_id, None)
            if approved_plan is not None:
                plan = f"v{approved_plan.plan.version} approved, {len(approved_plan.tasks)} tasks"
                plan_id: uuid.UUID | None = approved_plan.plan.id
            elif listed:
                plan, plan_id = f"v{listed[0].version} {listed[0].status} (not approved)", None
            else:
                plan, plan_id = "none", None
            return MissionView(row, architecture, plan, plan_id, self._repository(s, project_id))

    @staticmethod
    def _repository(s: Session, project_id: uuid.UUID) -> RepositoryView | None:
        git = git_api.get_git_service(s)
        try:
            settings = git.get(project_id)
        except SettingsNotFoundError:
            return None
        cloned = git.is_cloned(settings)
        branch = read_head(git.clone_path(settings))[0] if cloned else None
        return RepositoryView(
            provider=settings.provider,
            remote_url=settings.remote_url,
            clone_folder=settings.clone_folder,
            base_branch=settings.base_branch,
            branch_prefix=settings.branch_prefix,
            cloned=cloned,
            current_branch=branch,
            pat=pat_label(settings.pat_last4),
        )

    # ---- plan tasks and checklists ---------------------------------------------------------

    def tasks(self, project_id: uuid.UUID, plan_id: uuid.UUID) -> list[TaskRow]:
        with self._session() as s:
            detail = deps.get_plan_service(s).get(project_id, plan_id)
            statuses = deps.get_patch_review_service(s).task_statuses(project_id, plan_id)
            return [
                TaskRow(
                    key=t.key,
                    title=t.title,
                    kind=t.kind,
                    status=statuses.get(t.id, t.status),
                    depends_on=list(t.depends_on),
                    blocking_questions=len(t.blocking_questions),
                )
                for t in detail.tasks
            ]

    def checklist(self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str) -> ChecklistInfo:
        with self._session() as s:
            view = self._checklists(s).view(project_id, plan_id, task_key)
            current = view.checklist
            return ChecklistInfo(
                task_key=view.task_key,
                version=current.version if current else None,
                status=current.status if current else "none",
                next_step=view.next_step,
                steps=[
                    StepRow(
                        key=st.step.key,
                        title=st.step.title,
                        goal=st.step.goal,
                        depends_on=list(st.step.depends_on),
                        status=st.status,
                        resumable=st.resumable,
                        run_id=st.run.id if st.run else None,
                        patch_id=st.patch.id if st.patch else None,
                    )
                    for st in view.steps
                ],
            )

    def task_runs(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> list[agent_api.RunSummary]:
        with self._session() as s:
            agent = deps.make_agent_service(s, deps._NoGeneration())
            return [
                agent_api._summary(r) for r in agent.runs_for_task(project_id, plan_id, task_key)
            ]

    def _checklists(self, s: Session, provider: LLMProvider | None = None) -> ChecklistService:
        return checklists_api.get_checklist_service(s, provider or deps._NoGeneration())

    # ---- agent runs ------------------------------------------------------------------------

    def run(self, project_id: uuid.UUID, run_id: uuid.UUID) -> agent_api.RunResponse:
        """The run as the run panel shows it (the HTTP layer's own view)."""
        with self._session() as s:
            agent = deps.make_agent_service(s, deps._NoGeneration())
            run = agent.get_run(project_id, run_id)
            if run.status == "running" and not is_active(run.id):
                run = agent.mark_interrupted(project_id, run_id)
            return agent_api._full(
                run,
                agent.steps(project_id, run_id),
                agent.jobs(project_id, run_id),
                agent.hook_states(project_id),
                agent.ci_view(project_id, run),
            )

    def find_run(self, run_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, str]:
        """(project id, plan id, task key) of a run, for `archpilot code --resume`."""
        for project in self.projects():
            with self._session() as s:
                try:
                    found = deps.make_agent_service(s, deps._NoGeneration()).get_run(
                        project.id, run_id
                    )
                except RunNotFoundError:
                    continue
                return found.project_id, found.plan_id, found.task_key
        raise TuiError(f"Agent run {run_id} not found")

    def resume_run(self, project_id: uuid.UUID, run_id: uuid.UUID) -> uuid.UUID:
        """Resume a stopped or failed checklist step from its checkpoint (as the web UI does)."""
        with self._session() as s:
            run = deps.make_agent_service(s, deps._NoGeneration()).get_run(project_id, run_id)
            step, plan_id, task_key = run.checklist_step_key, run.plan_id, run.task_key
        if step is None:
            raise TuiError("Only checklist step runs can be resumed; start a new run instead")
        return self.resume_step(project_id, plan_id, task_key, step)

    def start_task_run(self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str) -> uuid.UUID:
        """A task-level run (tasks without an approved checklist), as POST .../agent-runs."""
        with self._session() as s:
            provider = self._provider()
            service = deps.make_agent_service(s, provider)
            run = service.start_run(project_id, plan_id, task_key)
            return self._launch(s, provider, project_id, run.id)

    def run_next_step(self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str) -> uuid.UUID:
        with self._session() as s:
            provider = self._provider()
            service = self._checklists(s, provider)
            run = service.run_next(project_id, plan_id, task_key)
            checklists_api._launch(service, s, provider, project_id, run, None)
            return run.id

    def resume_step(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, step_key: str
    ) -> uuid.UUID:
        with self._session() as s:
            provider = self._provider()
            service = self._checklists(s, provider)
            run = service.resume(project_id, plan_id, task_key, step_key)
            checklists_api._launch(service, s, provider, project_id, run, None)
            return run.id

    @staticmethod
    def _launch(
        s: Session, provider: LLMProvider, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> uuid.UUID:
        factory = sessionmaker(bind=s.get_bind(), expire_on_commit=False)
        run_provider = deps.provider_for_deployment(provider, None)

        def job() -> None:
            worker: Session = factory()
            try:
                deps.make_agent_service(worker, run_provider).execute(project_id, run_id)
            finally:
                worker.close()

        agent_api._spawn(job)
        return run_id

    # ---- change requests -------------------------------------------------------------------

    def submit_change(self, project_id: uuid.UUID, text: str, author: str) -> ChangeView:
        """Record a free-form change request and analyse it; a person approves it afterwards."""
        with self._session() as s:
            service = ChangeRequestService(s)
            cr = service.analyse(project_id, service.create(project_id, text, author).id)
            return ChangeView(
                key=cr.key,
                impact_class=effective_class(cr) or "",
                reason=str((cr.impact or {}).get("reason", "")),
                components=[str(c["name"]) for c in (cr.impact or {}).get("components", [])],
            )

    def stop_run(self, project_id: uuid.UUID, run_id: uuid.UUID) -> str:
        with self._session() as s:
            return deps.make_agent_service(s, deps._NoGeneration()).stop(project_id, run_id).status

    # ---- patches ---------------------------------------------------------------------------

    def patch(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> PatchView:
        with self._session() as s:
            detail = deps.get_patch_review_service(s).detail(project_id, patch_id)
            p = detail.patch
            coverage = p.coverage or {}
            missing = [
                f"{i.get('kind', '')} {i.get('ref', '')}: {', '.join(i.get('missing') or [])}"
                for i in coverage.get("items") or []
                if i.get("status") == "missing"
            ]
            return PatchView(
                id=p.id,
                task_key=p.task_key,
                version=p.version,
                status=p.status,
                files=[
                    PatchFileView(v.file.path, v.file.change, v.diff, v.diff_truncated)
                    for v in detail.views
                ],
                coverage={k: int(coverage.get(k, 0)) for k in ("covered", "missing", "unchecked")},
                missing=missing,
                removals=[
                    (str(i["file"]), str(i["name"]))
                    for i in coverage.get("removed_from_accepted") or []
                ],
                stale=[f"{f.code}: {f.path or ''}" for f in detail.stale],
                reviewed_by=p.reviewed_by,
                review_reason=p.review_reason,
                notes=list(p.notes),
            )

    def accept_target(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> Target:
        with self._session() as s:
            git = git_api.get_git_service(s)
            detail = deps.get_patch_review_service(s).detail(project_id, patch_id)
            action = (
                f"ACCEPT patch v{detail.patch.version} of {detail.patch.task_key} into the "
                "ArchPilotAI workspace (nothing is pushed)"
            )
            return self._target(s, git, project_id, action, detail.patch)

    def accept(
        self,
        project_id: uuid.UUID,
        patch_id: uuid.UUID,
        reviewer: str,
        reason: str,
        removal_reason: str = "",
    ) -> PatchView:
        with self._session() as s:
            service = deps.get_patch_review_service(s)
            current = self.patch(project_id, patch_id)
            confirmed = [(f, n, removal_reason) for f, n in current.removals]
            service.accept(project_id, patch_id, reviewer, reason, confirmed)
        return self.patch(project_id, patch_id)

    def reject(
        self, project_id: uuid.UUID, patch_id: uuid.UUID, reviewer: str, reason: str
    ) -> PatchView:
        with self._session() as s:
            deps.get_patch_review_service(s).reject(project_id, patch_id, reviewer, reason)
        return self.patch(project_id, patch_id)

    # ---- git: commit, push, pull request (explicit user actions only) ----------------------

    @staticmethod
    def _target(
        s: Session, git: Any, project_id: uuid.UUID, action: str, patch: Any | None
    ) -> Target:
        try:
            row = git.get(project_id)
        except SettingsNotFoundError:
            return Target(action, "(no repository configured)", "(none)", "(none)")
        branch = row.base_branch
        if patch is not None:
            task_title = git._patch(project_id, patch.id)[1]
            branch = git.branch_for(row, patch, task_title)
        return Target(action, row.clone_folder, row.remote_url, branch)

    def commit(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> str:
        with self._session() as s:
            git = git_api.get_git_service(s)
            result = git.commit_patch(project_id, patch_id)
            git_api._refresh_map(git, project_id)
            return str(result.branch)

    def push_target(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> Target:
        with self._session() as s:
            git = git_api.get_git_service(s)
            patch = deps.get_patch_review_service(s).detail(project_id, patch_id).patch
            target = self._target(s, git, project_id, "", patch)
            return Target(
                f"PUSH branch {target.branch} to the remote (never forced, never the base branch)",
                target.repository,
                target.remote,
                target.branch,
            )

    def push(self, project_id: uuid.UUID, branch: str, approved_by: str) -> str:
        with self._session() as s:
            return str(git_api.get_git_service(s).push(project_id, branch, approved_by))

    def pull_request_target(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> Target:
        with self._session() as s:
            git = git_api.get_git_service(s)
            patch = deps.get_patch_review_service(s).detail(project_id, patch_id).patch
            target = self._target(s, git, project_id, "", patch)
            row = git.get(project_id)
            return Target(
                f"OPEN PULL REQUEST {target.branch} -> {row.base_branch}",
                target.repository,
                target.remote,
                target.branch,
            )

    def pull_request(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> str:
        with self._session() as s:
            result = git_api.get_git_service(s).pull_request(project_id, patch_id)
            return f"{result.message} {result.url or ''}".strip()

    # ---- CI --------------------------------------------------------------------------------

    def ci(self, project_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any] | None:
        return self.run(project_id, run_id).ci_loop

    def ci_target(self, project_id: uuid.UUID, run_id: uuid.UUID) -> Target:
        with self._session() as s:
            info = deps.make_agent_service(s, deps._NoGeneration()).ci_approval_info(
                project_id, run_id
            )
            return Target(
                f"APPROVE CI BRANCH for run {run_id} (pushes only to this branch; "
                f"base {info['base_branch']} is protected)",
                str(info["repository"]),
                str(info["remote"]),
                str(info["branch"]),
            )

    def ci_approve(self, project_id: uuid.UUID, run_id: uuid.UUID, approved_by: str) -> str:
        with self._session() as s:
            deps.make_agent_service(s, deps._NoGeneration()).ci_approve(
                project_id, run_id, approved_by
            )
            return "approved"
