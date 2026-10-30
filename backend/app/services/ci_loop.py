"""Iterative CI repair loop of one approved checklist step (Phase 13E).

One explicit approval in the UI lets one agent run create and update one branch,
``<prefix>ci/<root-run-id>`` (the root run of a resume chain), and nothing else. After a batch of
edits the draft is committed on that branch through the Git Data API, the configured GitHub
Actions workflow is dispatched and polled, and its JUnit artifact is parsed and classified against
the unchanged repository baseline (one baseline workflow run at the base commit). The model gets
only what it should act on (app.domain.ci_loop.Classification.model_feed).

The agent and MCP have no tool for any of this: the agent loop calls `LoopRun.validate`, and the
person approves, revokes, stops and deletes the branch through the API. No Git command runs here;
every remote call goes through `_Api` (the token only in the Authorization header).

State that must outlive a request (approval, checkpoints, iteration results, cleanup) is stored
as AgentJob rows with the variants in LOOP_VARIANTS (command_id "ci"), summary JSON only: no
schema change. The live state below is per process, for the HTTP thread to see and wake the loop.
"""

import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.domain.ci_loop import (
    GREEN,
    NOT_CONVERGING,
    Classification,
    Classified,
    Convergence,
    ImportMap,
    approval_problem,
    classify,
    is_green,
    resume_problems,
)
from app.domain.execution import (
    FAILING,
    ProviderUnavailableError,
    check_command,
    parse_junit,
)
from app.models.agent import AgentJob, AgentRun
from app.repositories.agent import AgentRepository
from app.services.ci_execution import (
    ARTIFACT_NAME,
    HTTP_TIMEOUT_SECONDS,
    MAX_ARTIFACT_BYTES,
    PROJECT_TRAILER,
    RUN_TRAILER,
    WORKFLOW_FILE,
    CIError,
    CITarget,
    TargetLoader,
    WorkspaceLoader,
    _Api,
    _Cancelled,
    _cond,
    _scrub,
    _TimedOut,
    build_tree,
    check_ci_branch,
    ci_branch,
    draft_changes,
    junit_from_zip,
)

logger = logging.getLogger(__name__)

APPROVAL, CHECKPOINT, ITERATION, CLEANUP = (
    "ci_approval",
    "ci_checkpoint",
    "ci_iteration",
    "ci_cleanup",
)
LOOP_VARIANTS = frozenset({APPROVAL, CHECKPOINT, ITERATION, CLEANUP})
LOOP_COMMAND_ID = "ci"
ITERATION_TRAILER = "ArchPilot-Iteration"
PROTECTION_NOTICE = (
    "Only this exact branch can be created or updated. The base branch, the repository's default "
    "branch, main and master can never be changed; the default branch is checked before every push."
)
MAX_SUMMARIES = 20


class CILoopError(Exception):
    """A refused CI loop request (approval, revoke, branch deletion); safe to show."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code  # not_found | not_running | already_decided | not_ended | in_use | ...
        super().__init__(message)


class CIResumeRefusedError(Exception):
    """A CI checkpoint that cannot be resumed (HTTP 409)."""

    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        self.code = problems[0][0]
        self.codes = [c for c, _ in problems]
        super().__init__("Resume refused: " + "; ".join(m for _, m in problems))


# ---- live state ----------------------------------------------------------------------------------


@dataclass
class _Live:
    project_id: uuid.UUID
    run_id: uuid.UUID
    approval: str = "none"  # none | pending | approved | revoked
    approved_by: str | None = None
    approved_at: str | None = None
    cancelled: bool = False
    phase: str = "idle"
    branch: str | None = None
    iteration: int = 0
    commit: str | None = None
    workflow_run_id: int | None = None
    workflow_url: str | None = None
    workflow_status: str | None = None
    message: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "approval": self.approval,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "phase": self.phase,
            "branch": self.branch,
            "iteration": self.iteration,
            "commit": self.commit,
            "workflow_run_id": self.workflow_run_id,
            "workflow_url": self.workflow_url,
            "workflow_status": self.workflow_status,
            "message": self.message,
        }


_live: dict[uuid.UUID, _Live] = {}


def _state(project_id: uuid.UUID, run_id: uuid.UUID) -> _Live:
    with _cond:
        found = _live.get(run_id)
        if found is None or found.project_id != project_id:
            found = _live[run_id] = _Live(project_id, run_id)
        return found


def loop_state(project_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any] | None:
    """Live state of a running CI loop, only for its own project."""
    with _cond:
        found = _live.get(run_id)
        if found is None or found.project_id != project_id:
            return None
        return found.public()


def cancel_loop(run_id: uuid.UUID) -> None:
    """Stop: wakes polling and approval waits at once."""
    with _cond:
        found = _live.get(run_id)
        if found is not None:
            found.cancelled = True
            if found.approval in ("pending", "approved"):
                found.approval = "revoked"
        _cond.notify_all()


def grant_live(project_id: uuid.UUID, run_id: uuid.UUID, approved_by: str, at: str) -> None:
    """The person's approval reached the running loop (after the row was stored)."""
    with _cond:
        found = _state(project_id, run_id)
        if found.approval == "revoked" or found.cancelled:
            raise CILoopError("already_decided", "The CI approval of this run was revoked")
        found.approval, found.approved_by, found.approved_at = "approved", approved_by, at
        _cond.notify_all()


def revoke_live(project_id: uuid.UUID, run_id: uuid.UUID) -> None:
    with _cond:
        found = _live.get(run_id)
        if found is not None and found.project_id == project_id:
            found.approval = "revoked"
        _cond.notify_all()


def release_loop(run_id: uuid.UUID) -> None:
    with _cond:
        _live.pop(run_id, None)
        _cond.notify_all()


def _set(live: _Live, **values: Any) -> None:
    with _cond:
        for key, value in values.items():
            setattr(live, key, value)
        _cond.notify_all()


# ---- stored rows ---------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat()


def add_row(
    agents: AgentRepository,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    variant: str,
    data: dict[str, Any],
) -> AgentJob:
    row = AgentJob(
        id=uuid.uuid4(),
        run_id=run_id,
        project_id=project_id,
        command_id=LOOP_COMMAND_ID,
        variant=variant,
        select_test=None,
        status="done",
        timeout_seconds=0.0,
        exit_code=None,
        duration_ms=0,
        truncated=False,
        summary={"seq": time.time_ns(), **data},
        output="",
    )
    agents.add_jobs([row])
    agents.commit()
    return row


def rows(
    agents: AgentRepository, project_id: uuid.UUID, run_ids: Sequence[uuid.UUID], variant: str
) -> list[AgentJob]:
    found = agents.loop_rows(project_id, run_ids, variant)
    return sorted(found, key=lambda r: int((r.summary or {}).get("seq") or 0))


def update_row(agents: AgentRepository, row: AgentJob, **values: Any) -> None:
    row.summary = {**(row.summary or {}), **values}  # reassigned: JSON changes are tracked so
    agents.commit()


def chain(agents: AgentRepository, project_id: uuid.UUID, run_id: uuid.UUID) -> list[uuid.UUID]:
    """The run and the runs it resumed from, newest first (bounded)."""
    out: list[uuid.UUID] = []
    current: uuid.UUID | None = run_id
    while current is not None and current not in out and len(out) < 50:
        out.append(current)
        found = agents.get_run(project_id, current)
        current = found.resumed_from_run_id if found is not None else None
    return out


def latest_checkpoint(
    agents: AgentRepository, project_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, Any] | None:
    for rid in chain(agents, project_id, run_id):
        found = rows(agents, project_id, [rid], CHECKPOINT)
        if found:
            return dict(found[-1].summary or {})
    return None


def approval_record(
    agents: AgentRepository, project_id: uuid.UUID, run_id: uuid.UUID
) -> AgentJob | None:
    found = rows(agents, project_id, [run_id], APPROVAL)
    return found[-1] if found else None


# ---- configuration -------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopConfig:
    workflow: str = WORKFLOW_FILE
    command: str = "pytest"
    poll: float = 10.0
    ci_timeout: float = 900.0
    approval_timeout: float = 300.0
    max_iterations: int = 5
    max_edits: int = 5


@dataclass
class Validation:
    """What one validation request gave: the model feed and, when it ends the run, the reason."""

    status: str  # green | failing | static_failed | cached | stopped
    feed: dict[str, Any]
    terminal: str | None = None


class CILoop:
    """Builds the CI loop of a run; holds the project's target loader and the HTTP client."""

    def __init__(
        self,
        targets: TargetLoader,
        workspace: WorkspaceLoader,
        config: LoopConfig,
        *,
        client: httpx.Client | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        check_command(config.command)
        self._targets = targets
        self._workspace = workspace
        self.config = config
        self._client = client
        self._wall = wall

    def target(self, project_id: uuid.UUID) -> CITarget:
        return self._targets(project_id)

    def available(self, project_id: uuid.UUID) -> bool:
        try:
            self._targets(project_id)
        except ProviderUnavailableError:
            return False
        return True

    def accepted(self, project_id: uuid.UUID) -> Mapping[str, str]:
        return self._workspace(project_id)

    def client(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False), True

    def api(
        self,
        target: CITarget,
        client: httpx.Client,
        *,
        deadline: float,
        clock: Callable[[], float],
        cancelled: Callable[[], bool],
    ) -> _Api:
        return _Api(
            client, target, deadline=deadline, clock=clock, wall=self._wall, cancelled=cancelled
        )

    def branch_for(self, agents: AgentRepository, project_id: uuid.UUID, run_id: uuid.UUID) -> str:
        root = chain(agents, project_id, run_id)[-1]
        return ci_branch(self.target(project_id).branch_prefix, root)

    def branch_head(
        self, target: CITarget, branch: str, clock: Callable[[], float] = time.monotonic
    ) -> str | None:
        """The CI branch's head commit (None: no such branch)."""
        client, own = self.client()
        try:
            api = self.api(
                target, client, deadline=clock() + HTTP_TIMEOUT_SECONDS * 2, clock=clock,
                cancelled=lambda: False,
            )  # fmt: skip
            found = api.call(
                "GET", f"git/ref/heads/{quote(branch, safe='/')}", "the CI branch read",
                ok=(200, 404),
            )  # fmt: skip
            if found.status_code == 404:
                return None
            return str((api.json(found).get("object") or {}).get("sha") or "") or None
        finally:
            if own:
                client.close()

    def open_pulls(self, target: CITarget, branch: str) -> int:
        client, own = self.client()
        try:
            api = self.api(
                target, client, deadline=time.monotonic() + HTTP_TIMEOUT_SECONDS * 2,
                clock=time.monotonic, cancelled=lambda: False,
            )  # fmt: skip
            found = api.call(
                "GET", "pulls", "the pull request lookup",
                params={"head": f"{target.owner}:{branch}", "state": "open", "per_page": "5"},
            )  # fmt: skip
            try:
                data = found.json()
            except ValueError:
                return 0
            return len(data) if isinstance(data, list) else 0
        finally:
            if own:
                client.close()

    def delete_branch(self, target: CITarget, branch: str) -> None:
        client, own = self.client()
        try:
            api = self.api(
                target, client, deadline=time.monotonic() + HTTP_TIMEOUT_SECONDS * 2,
                clock=time.monotonic, cancelled=lambda: False,
            )  # fmt: skip
            api.call(
                "DELETE", f"git/refs/heads/{quote(branch, safe='/')}", "the CI branch deletion",
                ok=(204, 404, 422),
            )  # fmt: skip
        finally:
            if own:
                client.close()

    def check_resume(
        self,
        agents: AgentRepository,
        project_id: uuid.UUID,
        resume_from: uuid.UUID,
        *,
        checklist_id: uuid.UUID | None,
        checklist_version: int | None,
    ) -> None:
        """Raise CIResumeRefusedError when the latest CI checkpoint of the chain cannot go on."""
        checkpoint = latest_checkpoint(agents, project_id, resume_from)
        if checkpoint is None:
            return
        try:
            target = self.target(project_id)
        except ProviderUnavailableError as exc:
            raise CIResumeRefusedError([("ci_unavailable", str(exc))]) from None
        try:
            head = self.branch_head(target, str(checkpoint.get("branch") or ""))
        except CIError as exc:
            raise CIResumeRefusedError(
                [("ci_unavailable", _scrub(str(exc), target.token))]
            ) from None
        problems = resume_problems(
            checkpoint,
            remote=target.html_repo,
            base_branch=target.base_branch,
            checklist_id=str(checklist_id) if checklist_id else None,
            checklist_version=checklist_version,
            branch_head=head,
        )
        if problems:
            raise CIResumeRefusedError(problems)

    def start(
        self,
        agents: AgentRepository,
        run: AgentRun,
        *,
        clock: Callable[[], float],
        deadline: float,
        checklist_version: int | None,
    ) -> "LoopRun":
        return LoopRun(self, agents, run, clock=clock, deadline=deadline, version=checklist_version)


# ---- one run -------------------------------------------------------------------------------------


@dataclass
class _Report:
    workflow_run_id: int
    url: str
    conclusion: str
    cases: list[Any] | None


@dataclass
class LoopRun:
    loop: CILoop
    agents: AgentRepository
    run: AgentRun
    clock: Callable[[], float]
    deadline: float
    version: int | None
    branch: str = ""
    root_id: uuid.UUID | None = None
    base_sha: str | None = None
    head: str | None = None
    baseline_failing: list[str] | None = None
    baseline_conclusion: str | None = None
    iteration: int = 0
    convergence: Convergence = field(default_factory=Convergence)
    previous_failing: list[str] = field(default_factory=list)
    seen_runs: set[int] = field(default_factory=set)
    last_sig: str | None = None
    last: Validation | None = None
    green_sig: str | None = None
    terminal: str | None = None
    message: str | None = None
    latest: dict[str, Any] = field(default_factory=dict)
    fixed: list[str] = field(default_factory=list)
    workflow_url: str | None = None
    test_summary: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.project_id = self.run.project_id
        self.run_id = self.run.id
        ids = chain(self.agents, self.project_id, self.run_id)
        self.root_id = ids[-1]
        self.live = _state(self.project_id, self.run_id)
        restored = latest_checkpoint(self.agents, self.project_id, self.run_id)
        if restored is not None:
            self._restore(restored)

    # ---- state ---------------------------------------------------------------------------------

    def _restore(self, data: Mapping[str, Any]) -> None:
        self.base_sha = data.get("base_sha")
        self.head = data.get("pushed_commit") or data.get("parent_commit")
        failing = data.get("baseline_failing")
        self.baseline_failing = list(failing) if isinstance(failing, list) else None
        self.baseline_conclusion = data.get("baseline_conclusion")
        self.iteration = int(data.get("iteration") or 0)
        self.convergence = Convergence.from_json(data.get("convergence"))
        self.previous_failing = list(data.get("previous_failing") or [])
        self.seen_runs = {int(r) for r in data.get("seen_runs") or []}
        self.fixed = list(data.get("fixed") or [])
        _set(self.live, iteration=self.iteration, commit=self.head)

    def _branch(self, target: CITarget) -> str:
        if not self.branch:
            assert self.root_id is not None
            self.branch = ci_branch(target.branch_prefix, self.root_id)
            _set(self.live, branch=self.branch)
        return self.branch

    def revoked(self) -> bool:
        with _cond:
            return self.live.approval == "revoked"

    def _cancelled(self) -> bool:
        with _cond:
            return self.live.cancelled or self.live.approval == "revoked"

    def stop_reason(self) -> str:
        with _cond:
            if self.live.cancelled:
                return "cancelled"
        return "approval_revoked" if self.revoked() else "cancelled"

    def _end(self, reason: str, message: str) -> Validation:
        self.terminal, self.message = reason, message
        _set(self.live, phase=reason, message=message)
        logger.info("ci loop ended run=%s reason=%s", self.run_id, reason)
        return Validation(
            "stopped", {"status": "stopped", "reason": reason, "message": message}, reason
        )

    def view(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "terminal": self.terminal,
            "message": self.message,
            "green": self.green_sig is not None,
        }

    # ---- approval ------------------------------------------------------------------------------

    def _await_approval(self) -> bool:
        end = self.clock() + self.loop.config.approval_timeout
        with _cond:
            if self.live.approval == "none":
                self.live.approval, self.live.phase = "pending", "awaiting_approval"
                _cond.notify_all()
            while True:
                if self.live.cancelled or self.live.approval == "revoked":
                    return False
                if self.live.approval == "approved":
                    return True
                remaining = min(end, self.deadline) - self.clock()
                if remaining <= 0:
                    return False
                _cond.wait(min(remaining, 0.5))

    def _permitted(self, target: CITarget, default_branch: str) -> str | None:
        found = approval_record(self.agents, self.project_id, self.run_id)
        return approval_problem(
            found.summary if found is not None else None,
            project_id=str(self.project_id),
            run_id=str(self.run_id),
            branch=self.branch,
            base_branch=target.base_branch,
            default_branch=default_branch,
        )

    # ---- validation ----------------------------------------------------------------------------

    def validate(
        self,
        *,
        files: Mapping[str, str],
        draft_sig: str,
        static_total: int,
        summaries: Sequence[str],
        file_hashes: Mapping[str, str | None],
    ) -> Validation:
        """Static gate, then commit, push, dispatch, poll and classify one CI iteration."""
        if self.terminal is not None:
            return self._end(self.terminal, self.message or self.terminal)
        if self._cancelled():
            return self._end(self.stop_reason(), "Stopped before the CI push")
        if draft_sig == self.last_sig and self.last is not None:
            # Same draft: the same workflow run's result, not a new iteration.
            return Validation("cached", {**self.last.feed, "cached": True}, None)
        if static_total > 0:
            return Validation(
                "static_failed",
                {
                    "status": "static_failed",
                    "static_findings": static_total,
                    "message": "Static checks have findings; nothing was pushed to CI. Fix them "
                    "first.",
                },
            )
        if self.iteration >= self.loop.config.max_iterations:
            return self._end("step_limit", "The maximum number of CI iterations was reached")
        try:
            target = self.loop.target(self.project_id)
        except ProviderUnavailableError as exc:
            return self._end("ci_unavailable", str(exc))
        branch = self._branch(target)
        # A job can wait minutes: hold no write transaction meanwhile.
        self.agents.commit()
        if not self._await_approval():
            with _cond:
                stopped = self.live.cancelled
            if stopped:
                return self._end("cancelled", "Stopped while waiting for the CI approval")
            if self.clock() >= self.deadline:
                return self._end("time_limit", "The run's time limit was reached")
            return self._end("approval_revoked", "The CI branch push was not approved")
        client, own = self.loop.client()
        api = self.loop.api(
            target,
            client,
            deadline=min(self.clock() + self.loop.config.ci_timeout, self.deadline),
            clock=self.clock,
            cancelled=self._cancelled,
        )
        workflow_run: list[int] = []
        try:
            return self._iterate(
                api, target, branch, files, draft_sig, summaries, file_hashes, workflow_run
            )
        except _Cancelled:
            self._cancel_remote(api, workflow_run)
            return self._end(self.stop_reason(), "Stopped during the CI iteration")
        except _TimedOut:
            self._cancel_remote(api, workflow_run)
            if self.clock() >= self.deadline:
                return self._end("time_limit", "The run's time limit was reached while in CI")
            return self._end("ci_timeout", "The CI workflow did not finish in time")
        except (CIError, ProviderUnavailableError) as exc:
            self._cancel_remote(api, workflow_run)
            return self._end("ci_unavailable", _scrub(str(exc), target.token))
        finally:
            if own:
                client.close()

    def _iterate(
        self,
        api: _Api,
        target: CITarget,
        branch: str,
        files: Mapping[str, str],
        draft_sig: str,
        summaries: Sequence[str],
        file_hashes: Mapping[str, str | None],
        workflow_run: list[int],
    ) -> Validation:
        _set(self.live, phase="checking")
        repo = api.json(api.call("GET", "", "the repository read"))
        default_branch = str(repo.get("default_branch") or target.base_branch)
        check_ci_branch(
            branch,
            prefix=target.branch_prefix,
            base_branch=target.base_branch,
            default_branch=default_branch,
        )
        problem = self._permitted(target, default_branch)
        if problem is not None:
            return self._end("approval_revoked", f"Push refused: {problem}")
        accepted = self.loop.accepted(self.project_id)
        upserts, deletions = draft_changes(accepted, files)
        if self.baseline_failing is None:
            self._baseline(api, target, branch, workflow_run, summaries)
        checkpoint = self._checkpoint(target, summaries, file_hashes)
        api.check()  # no commit after a cancel
        commit = self._commit(api, target, branch, upserts, deletions)
        update_row(self.agents, checkpoint, pushed_commit=commit)
        report = self._run_workflow(api, target, branch, commit, workflow_run)
        if report.workflow_run_id in self.seen_runs:
            return Validation("cached", {**(self.last.feed if self.last else {}), "cached": True})
        self.seen_runs.add(report.workflow_run_id)
        self.iteration += 1
        changed = sorted(set(upserts) | set(deletions))
        result = classify(
            report.cases or [],
            baseline_failing=self.baseline_failing,
            previous_failing=self.previous_failing,
            changed_paths=changed,
            imports=ImportMap(files),
            scrub=lambda text: _scrub(text, target.token),
        )
        green = is_green(result, report.conclusion, report.cases is not None)
        self.previous_failing = [
            c.name for c in report.cases or [] if c.outcome in FAILING and not c.flaky
        ]
        self.fixed = sorted(
            {*self.fixed, *(r.name for r in result.results if r.novelty == "fixed")}
        )
        self.workflow_url = report.url
        self.test_summary = _totals(report.cases)
        self.convergence.add(
            report.workflow_run_id, result.blocking_signatures, len(result.blocking_signatures)
        )
        feed = {
            "status": "green" if green else "failing",
            "iteration": self.iteration,
            "max_iterations": self.loop.config.max_iterations,
            "conclusion": report.conclusion,
            "report": report.cases is not None,
            **result.model_feed(),
        }
        self.latest = _stored(result, report, self.iteration, commit, green)
        add_row(self.agents, self.project_id, self.run_id, ITERATION, self.latest)
        update_row(
            self.agents,
            checkpoint,
            latest_failures=self.latest["results"][:50],
            iteration=self.iteration,
            convergence=self.convergence.as_json(),
            previous_failing=self.previous_failing[:500],
            seen_runs=sorted(self.seen_runs),
            fixed=self.fixed[:200],
        )
        _set(self.live, phase="green" if green else "failing", iteration=self.iteration)
        validation = Validation("green" if green else "failing", feed)
        self.last_sig, self.last = draft_sig, validation
        if green:
            self.green_sig = draft_sig
            return validation
        if self.convergence.not_converging():
            ended = self._end(
                NOT_CONVERGING,
                "The same failure signature appeared in 3 CI iterations without improvement",
            )
            ended.feed.update(feed)
            return ended
        if self.iteration >= self.loop.config.max_iterations:
            ended = self._end("step_limit", "The maximum number of CI iterations was reached")
            ended.feed.update(feed)
            return ended
        return validation

    # ---- steps ---------------------------------------------------------------------------------

    def _checkpoint(
        self,
        target: CITarget,
        summaries: Sequence[str],
        file_hashes: Mapping[str, str | None],
    ) -> AgentJob:
        """Stored before each CI commit; a stopped run resumes from the latest one."""
        run = self.run
        return add_row(
            self.agents,
            self.project_id,
            self.run_id,
            CHECKPOINT,
            {
                "branch": self.branch,
                "root_run_id": str(self.root_id),
                "remote": target.html_repo,
                "base_branch": target.base_branch,
                "base_sha": self.base_sha,
                "parent_commit": self.head,
                "pushed_commit": None,
                "checklist_id": str(run.checklist_id) if run.checklist_id else None,
                "checklist_version": self.version,
                "step_key": run.checklist_step_key,
                "draft_hashes": dict(sorted(file_hashes.items())[:2000]),
                "actions": [s[:200] for s in list(summaries)[-MAX_SUMMARIES:]],
                "latest_failures": (self.latest.get("results") or [])[:50],
                "iteration": self.iteration,
                "deployment": run.deployment,
                "tokens": {"input": run.input_tokens, "output": run.output_tokens},
                "baseline_failing": self.baseline_failing,
                "baseline_conclusion": self.baseline_conclusion,
                "convergence": self.convergence.as_json(),
                "previous_failing": self.previous_failing[:500],
                "seen_runs": sorted(self.seen_runs),
                "fixed": self.fixed[:200],
            },
        )

    def _ref_head(self, api: _Api, branch: str) -> str | None:
        found = api.call(
            "GET", f"git/ref/heads/{quote(branch, safe='/')}", "the CI branch read", ok=(200, 404)
        )
        if found.status_code == 404:
            return None
        return str((api.json(found).get("object") or {}).get("sha") or "") or None

    def _baseline(
        self,
        api: _Api,
        target: CITarget,
        branch: str,
        workflow_run: list[int],
        summaries: Sequence[str],
    ) -> None:
        """The unchanged repository: the CI branch is created at the base commit and the
        workflow runs there once."""
        _set(self.live, phase="baseline")
        base_ref = quote(target.base_branch, safe="/")
        head = api.json(api.call("GET", f"git/ref/heads/{base_ref}", "the base branch read"))
        base_sha = str((head.get("object") or {}).get("sha") or "")
        if not base_sha:
            raise CIError("The base branch has no commit")
        current = self._ref_head(api, branch)
        if current is None:
            api.check()
            api.call(
                "POST", "git/refs", "the CI branch creation", ok=(201,),
                json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            )  # fmt: skip
        elif current != base_sha:
            raise CIError("Refused: the CI branch exists with commits this run did not create")
        self.base_sha = self.head = base_sha
        _set(self.live, commit=base_sha)
        report = self._run_workflow(api, target, branch, base_sha, workflow_run)
        self.seen_runs.add(report.workflow_run_id)
        cases = report.cases or []
        self.baseline_failing = sorted(
            c.name for c in cases if c.outcome in FAILING and not c.flaky
        )
        self.baseline_conclusion = report.conclusion
        add_row(
            self.agents,
            self.project_id,
            self.run_id,
            ITERATION,
            {
                "iteration": 0,
                "baseline": True,
                "commit": base_sha,
                "workflow_run_id": report.workflow_run_id,
                "workflow_url": report.url,
                "conclusion": report.conclusion,
                "report": report.cases is not None,
                "failing": len(self.baseline_failing),
                "totals": _totals(report.cases),
            },
        )

    def _commit(
        self,
        api: _Api,
        target: CITarget,
        branch: str,
        upserts: Mapping[str, str],
        deletions: list[str],
    ) -> str:
        _set(self.live, phase="pushing")
        current = self._ref_head(api, branch)
        if current is None or current != self.head:
            raise CIError("Refused: the CI branch has commits that this run did not create")
        base = api.json(api.call("GET", f"git/commits/{self.base_sha}", "the base commit read"))
        base_tree = str((base.get("tree") or {}).get("sha") or "")
        tree_sha, _ = build_tree(api, base_tree, upserts, deletions)
        message = (
            f"ArchPilotAI CI iteration {self.iteration + 1}\n\n"
            f"{PROJECT_TRAILER}: {self.project_id}\n{RUN_TRAILER}: {self.run_id}\n"
            f"{ITERATION_TRAILER}: {self.iteration + 1}\n"
        )
        api.check()
        made = api.call(
            "POST", "git/commits", "the commit creation", ok=(201,),
            json={"message": message, "tree": tree_sha, "parents": [self.head]},
        )  # fmt: skip
        new_sha = str(api.json(made).get("sha") or "")
        api.call(
            "PATCH", f"git/refs/heads/{quote(branch, safe='/')}", "the CI branch update",
            json={"sha": new_sha, "force": False},
        )  # fmt: skip
        self.head = new_sha
        _set(self.live, commit=new_sha)
        logger.info("ci loop pushed run=%s iteration=%d", self.run_id, self.iteration + 1)
        return new_sha

    def _run_workflow(
        self, api: _Api, target: CITarget, branch: str, commit: str, workflow_run: list[int]
    ) -> _Report:
        workflow = quote(self.loop.config.workflow, safe="")
        api.call(
            "POST", f"actions/workflows/{workflow}/dispatches", "the workflow dispatch",
            ok=(200, 204),
            json={
                "ref": branch,
                "inputs": {"command": self.loop.config.command, "archpilot_run": str(self.run_id)},
            },
        )  # fmt: skip
        _set(self.live, phase="queued", workflow_status="queued")
        number = self._find_run(api, workflow, branch, commit)
        workflow_run[:] = [number]
        url = f"{target.html_repo}/actions/runs/{number}"
        _set(self.live, workflow_run_id=number, workflow_url=url)
        conclusion = self._wait(api, number)
        if conclusion == "cancelled":
            raise CIError("The workflow run was cancelled on GitHub")
        if conclusion == "timed_out":
            raise _TimedOut
        _set(self.live, phase="downloading", workflow_status=f"completed: {conclusion}")
        xml = self._artifact(api, number)
        cases = None
        if xml is not None:
            try:
                cases = parse_junit(xml)
            except ValueError:
                cases = None
        return _Report(number, url, conclusion, cases)

    def _find_run(self, api: _Api, workflow: str, branch: str, commit: str) -> int:
        while True:
            found = api.json(
                api.call(
                    "GET",
                    f"actions/workflows/{workflow}/runs",
                    "the workflow run lookup",
                    params={
                        "branch": branch,
                        "event": "workflow_dispatch",
                        "head_sha": commit,
                        "per_page": "10",
                    },
                )  # fmt: skip
            )
            for item in found.get("workflow_runs") or []:
                if isinstance(item, dict) and item.get("head_sha") == commit:
                    number = item.get("id")
                    if isinstance(number, int):
                        return number
            api.sleep(self.loop.config.poll)

    def _wait(self, api: _Api, number: int) -> str:
        while True:
            data = api.json(api.call("GET", f"actions/runs/{number}", "the workflow status"))
            status = str(data.get("status") or "")
            if status == "completed":
                return str(data.get("conclusion") or "unknown")
            _set(self.live, workflow_status=status or "queued", phase="polling")
            api.sleep(self.loop.config.poll)

    def _artifact(self, api: _Api, number: int) -> str | None:
        listing = api.json(
            api.call(
                "GET",
                f"actions/runs/{number}/artifacts",
                "the artifact list",
                params={"name": ARTIFACT_NAME},
            )  # fmt: skip
        )
        for item in listing.get("artifacts") or []:
            if (
                isinstance(item, dict)
                and item.get("name") == ARTIFACT_NAME
                and not item.get("expired")
            ):
                size = item.get("size_in_bytes")
                if isinstance(size, int) and size > MAX_ARTIFACT_BYTES:
                    raise CIError("The test artifact is too large")
                return junit_from_zip(api.download(str(item.get("archive_download_url") or "")))
        return None

    def _cancel_remote(self, api: _Api, workflow_run: list[int]) -> None:
        """Cancel the GitHub workflow run in flight (best effort; no deadline)."""
        if not workflow_run:
            return
        try:
            api.call(
                "POST", f"actions/runs/{workflow_run[0]}/cancel", "the workflow cancel",
                ok=(202, 409), bounded=False,
            )  # fmt: skip
            _set(self.live, workflow_status="cancel requested")
        except CIError:
            logger.info("ci loop workflow not cancelled run=%s", self.run_id)

    # ---- result notes --------------------------------------------------------------------------

    def green_notes(self, warnings: Sequence[str]) -> list[str]:
        latest = self.latest
        totals = self.test_summary
        notes = [
            f"CI green: {self.loop.config.workflow} on {self.branch}",
            f"CI workflow: {self.workflow_url}",
            f"Final commit: {self.head}",
            f"CI iterations: {self.iteration}",
            "Tests: " + ", ".join(f"{totals[k]} {k}" for k in sorted(totals)) if totals else
            "Tests: no JUnit report (workflow conclusion success)",
        ]  # fmt: skip
        if self.fixed:
            notes.append("Fixed failures: " + ", ".join(self.fixed[:20]))
        results = [Classified.from_json(r) for r in latest.get("results") or []]
        pre = [r.name for r in results if r.novelty == "pre_existing"]
        flaky = [r.name for r in results if r.novelty == "flaky"]
        if pre:
            notes.append("Non-blocking: pre-existing failures: " + ", ".join(pre[:20]))
        if flaky:
            notes.append("Non-blocking: flaky tests: " + ", ".join(flaky[:20]))
        notes += [f"Non-blocking warning: {w}" for w in list(warnings)[:5]]
        return notes

    def stop_notes(self, reason: str) -> list[str]:
        notes = [
            f"Stopped: {reason}. {self.message or ''}".strip(),
            f"CI branch: {self.branch}; last commit: {self.head}; iterations: {self.iteration}",
        ]
        if self.workflow_url:
            notes.append(f"Last CI workflow: {self.workflow_url}")
        remaining = [
            Classified.from_json(r)
            for r in self.latest.get("results") or []
            if r.get("novelty") == "new"
        ]
        notes += [
            f"Remaining failure: {r.name} ({r.category}) {r.message}"[:300] for r in remaining[:15]
        ]
        return notes


def _totals(cases: Sequence[Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for case in cases or []:
        out[case.outcome] = out.get(case.outcome, 0) + 1
    return out


def _stored(
    result: Classification, report: _Report, iteration: int, commit: str, green: bool
) -> dict[str, Any]:
    return {
        "iteration": iteration,
        "commit": commit,
        "workflow_run_id": report.workflow_run_id,
        "workflow_url": report.url,
        "conclusion": report.conclusion,
        "report": report.cases is not None,
        "green": green,
        "counts": result.counts,
        "totals": _totals(report.cases),
        "results": [r.as_json() for r in result.results[:200]],
        "actionable": [r.signature for r in result.actionable],
    }


def classify_counts(stored: Mapping[str, Any] | None) -> dict[str, int]:
    counts = dict((stored or {}).get("counts") or {})
    return {
        k: int(counts.get(k, 0)) for k in ("new", "pre_existing", "fixed", "flaky", "elsewhere")
    }


__all__ = [
    "APPROVAL",
    "CHECKPOINT",
    "CLEANUP",
    "GREEN",
    "ITERATION",
    "LOOP_VARIANTS",
    "PROTECTION_NOTICE",
    "CILoop",
    "CILoopError",
    "CIResumeRefusedError",
    "LoopConfig",
    "LoopRun",
    "Validation",
    "approval_record",
    "cancel_loop",
    "loop_state",
    "release_loop",
]
