"""GitHub Actions execution provider (Phase 13B).

A test command runs in the target repository's own CI, never on this machine:

1. the user approves the CI push once per run, in the run panel (the agent cannot);
2. the draft's changes against the accepted workspace are committed on top of the base branch
   through the Git Data API and pushed to a dedicated branch ``<prefix>ci/<run-id>`` (never the
   base branch, a protected name or the repository's default branch);
3. the ``archpilot-tests.yml`` workflow is dispatched on that branch (workflow_dispatch), its run
   is polled until it completes or the job times out / is cancelled;
4. the ``archpilot-junit`` artifact is downloaded and its JUnit XML returned as the report (the
   test runner parses it with the existing parser);
5. the CI branch is deleted unless the user chose to keep it.

The personal access token is read from the credential store for each job. It is sent only in the
Authorization header to the provider's API host; never in a URL, a log line, a message or the
stored job summary. Artifact downloads are redirected to storage hosts: the redirect is followed
by hand, over HTTPS only and without the Authorization header. The workflow file itself is a
template the user adds to the repository; nothing here writes under ``.github/``.
"""

import hashlib
import io
import logging
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy.orm import Session

from app.domain.execution import (
    MAX_REPORT_BYTES,
    ExecutionResult,
    ProviderUnavailableError,
    UnknownCommandError,
    check_command,
    make_result,
)
from app.domain.git_remote import (
    GitSettingsError,
    RemoteRef,
    parse_remote,
    validate_branch_name,
)
from app.domain.patch import path_problem
from app.models.git_remote import GitRemoteSettings
from app.services.credential_store import CredentialStore, CredentialStoreUnavailableError

logger = logging.getLogger(__name__)

PROVIDER_NAME = "github_actions"
WORKFLOW_FILE = "archpilot-tests.yml"
ARTIFACT_NAME = "archpilot-junit"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "ci" / WORKFLOW_FILE
PROTECTED_NAMES = frozenset({"main", "master"})
SUPPORTED_PROVIDERS = frozenset({"github", "github_enterprise"})
HTTP_TIMEOUT_SECONDS = 15.0
MAX_ARTIFACT_BYTES = 20_000_000  # the zip as downloaded
MAX_RATE_RETRIES = 3
MAX_COMMIT_FILES = 2_000
RUN_TRAILER = "ArchPilot-Run"
PROJECT_TRAILER = "ArchPilot-Project"
JOB_TRAILER = "ArchPilot-Job"
DENIED_MESSAGE = "CI push not approved by the user"


def workflow_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


class CIError(Exception):
    """A refusal or failure of a CI job; the message is safe to show (no token, no body)."""


class CIApprovalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code  # not_found | not_pending
        super().__init__(message)


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


@dataclass(frozen=True)
class CITarget:
    """Where CI runs: the project's GitHub repository and its token (kept out of repr)."""

    provider: str
    remote: RemoteRef
    base_branch: str
    branch_prefix: str
    token: str = field(repr=False)

    @property
    def owner(self) -> str:
        return self.remote.segments[0]

    @property
    def repo(self) -> str:
        return self.remote.segments[1]

    @property
    def api_base(self) -> str:
        if self.provider == "github":
            return "https://api.github.com"
        return f"https://{self.remote.host}/api/v3"

    @property
    def repo_api(self) -> str:
        return f"{self.api_base}/repos/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}"

    @property
    def html_repo(self) -> str:
        host = "github.com" if self.provider == "github" else self.remote.host
        return f"https://{host}/{quote(self.owner, safe='')}/{quote(self.repo, safe='')}"


TargetLoader = Callable[[uuid.UUID], CITarget]
WorkspaceLoader = Callable[[uuid.UUID], Mapping[str, str]]


def ci_branch(prefix: str, run_id: uuid.UUID) -> str:
    return f"{prefix}ci/{run_id}"


def check_ci_branch(branch: str, *, prefix: str, base_branch: str, default_branch: str) -> str:
    """The CI branch must be the dedicated one, and never the base or default branch."""
    try:
        validate_branch_name(branch, "CI branch")
    except GitSettingsError as exc:
        raise CIError(str(exc)) from None
    name = branch.lower()
    forbidden = {base_branch.lower(), default_branch.lower()} | PROTECTED_NAMES
    if name in forbidden:
        raise CIError("Refused: the CI branch would be the base or default branch")
    if not branch.startswith(f"{prefix}ci/"):
        raise CIError("Refused: the CI branch must be under the <prefix>ci/ namespace")
    return branch


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def git_blob_sha(content: str) -> str:
    data = content.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def junit_from_zip(data: bytes) -> str | None:
    """The JUnit XML of an artifact zip: ``junit.xml`` or the first ``.xml`` member. Sizes are
    checked before reading (zip bombs); anything that is not a readable zip gives None."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        return None
    with archive:
        members = [
            m for m in archive.infolist() if not m.is_dir() and m.filename.lower().endswith(".xml")
        ]
        members.sort(key=lambda m: (m.filename.rsplit("/", 1)[-1] != "junit.xml", m.filename))
        for member in members:
            if member.file_size > MAX_REPORT_BYTES:
                continue
            try:
                with archive.open(member) as handle:
                    raw = handle.read(MAX_REPORT_BYTES + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
                continue
            if len(raw) > MAX_REPORT_BYTES:
                continue
            return raw.decode("utf-8", errors="replace")
    return None


# ---- per-run approval and live state -----------------------------------------------------------
# The approval arrives on an HTTP thread while the job waits on the agent thread, so the state
# lives here, keyed by run id, with its project id for isolation. Nothing here is persisted:
# what a job did is stored in its AgentJob summary (JSON).

_cond = threading.Condition()


@dataclass
class _RunState:
    project_id: uuid.UUID
    run_id: uuid.UUID
    approval: str = "none"  # none | pending | approved | denied
    approved_by: str | None = None
    keep_branches: bool = False
    phase: str = "idle"
    command_id: str | None = None
    branch: str | None = None
    workflow_run_id: int | None = None
    workflow_url: str | None = None
    conclusion: str | None = None
    message: str | None = None
    cancelled_jobs: set[str] = field(default_factory=set)

    def public(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER_NAME,
            "approval": self.approval,
            "approved_by": self.approved_by,
            "keep_branches": self.keep_branches,
            "phase": self.phase,
            "command_id": self.command_id,
            "branch": self.branch,
            "workflow_run_id": self.workflow_run_id,
            "workflow_url": self.workflow_url,
            "conclusion": self.conclusion,
            "message": self.message,
        }


_states: dict[uuid.UUID, _RunState] = {}


def _state(project_id: uuid.UUID, run_id: uuid.UUID) -> _RunState:
    with _cond:
        found = _states.get(run_id)
        if found is None or found.project_id != project_id:
            found = _states[run_id] = _RunState(project_id, run_id)
        return found


def ci_state(project_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any] | None:
    """Live CI state of a running run, only for its own project."""
    with _cond:
        found = _states.get(run_id)
        if found is None or found.project_id != project_id:
            return None
        return found.public()


def decide(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    approve: bool,
    approved_by: str,
    keep_branches: bool = False,
) -> dict[str, Any]:
    """The user's answer to the pending CI push of a run. Once per run: an approval covers the
    run's later CI jobs; a refusal refuses them too."""
    with _cond:
        found = _states.get(run_id)
        if found is None or found.project_id != project_id:
            raise CIApprovalError("not_found", "No CI job of this run is waiting")
        if found.approval != "pending":
            raise CIApprovalError("not_pending", "No CI push is waiting for approval")
        found.approval = "approved" if approve else "denied"
        found.approved_by = approved_by
        found.keep_branches = bool(keep_branches) and approve
        _cond.notify_all()
        logger.info("ci approval run=%s approved=%s", run_id, approve)
        return found.public()


def release_state(run_id: uuid.UUID) -> None:
    with _cond:
        _states.pop(run_id, None)
        _cond.notify_all()


# ---- GitHub API calls of one job ---------------------------------------------------------------


class _Api:
    def __init__(
        self,
        client: httpx.Client,
        target: CITarget,
        *,
        deadline: float,
        clock: Callable[[], float],
        wall: Callable[[], float],
        cancelled: Callable[[], bool],
    ) -> None:
        self._client = client
        self._target = target
        self._deadline = deadline
        self._clock = clock
        self._wall = wall
        self._cancelled = cancelled

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + self._target.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def check(self) -> None:
        if self._cancelled():
            raise _Cancelled
        if self._clock() >= self._deadline:
            raise _TimedOut

    def sleep(self, seconds: float) -> None:
        """Wait, waking at once on a cancel; then check the deadline."""
        end = self._clock() + max(seconds, 0.0)
        with _cond:
            while not self._cancelled():
                remaining = min(end, self._deadline) - self._clock()
                if remaining <= 0:
                    break
                _cond.wait(min(remaining, 0.5))
        self.check()

    def _rate_wait(self, response: httpx.Response) -> float | None:
        if response.status_code not in (403, 429):
            return None
        retry = response.headers.get("retry-after", "")
        if retry.isdigit():
            return float(retry)
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "")
            return max(float(reset) - self._wall(), 0.0) if reset.isdigit() else 60.0
        return 60.0 if response.status_code == 429 else None

    def call(
        self,
        method: str,
        path: str,
        action: str,
        *,
        ok: tuple[int, ...] = (200,),
        json: Any = None,
        params: Mapping[str, str] | None = None,
        bounded: bool = True,
    ) -> httpx.Response:
        """One API call (path relative to the repository API). `bounded=False` is for cleanup
        after a cancel or timeout: no deadline, no rate-limit wait."""
        url = f"{self._target.repo_api}/{path}" if path else self._target.repo_api
        for attempt in range(MAX_RATE_RETRIES + 1):
            if bounded:
                self.check()
            try:
                response = self._client.request(
                    method, url, headers=self._headers, json=json, params=params
                )
            except httpx.HTTPError as exc:
                raise CIError(f"The GitHub API is unreachable ({type(exc).__name__})") from None
            wait = self._rate_wait(response) if bounded else None
            if wait is None:
                break
            if attempt == MAX_RATE_RETRIES or self._clock() + wait >= self._deadline:
                raise CIError(f"GitHub API rate limit reached during {action}; try again later")
            logger.info("ci rate limited action=%s wait=%.0fs", action, wait)
            self.sleep(wait)
        if response.status_code not in ok:
            hint = " (check the token's scopes)" if response.status_code in (401, 403) else ""
            raise CIError(f"GitHub refused {action}: HTTP {response.status_code}{hint}")
        return response

    def json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def download(self, url: str) -> bytes:
        """An artifact archive. Only from the API host with the token; the storage redirect is
        followed without it, over HTTPS only."""
        if not url.startswith(self._target.api_base + "/"):
            raise CIError("Refused: the artifact URL is not on the GitHub API host")
        current, headers = url, self._headers
        for _ in range(3):
            self.check()
            try:
                with self._client.stream(
                    "GET", current, headers=headers, follow_redirects=False
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location", "")
                        if urlsplit(location).scheme != "https":
                            raise CIError("Refused: the artifact redirect is not HTTPS")
                        current, headers = location, {}
                        continue
                    if response.status_code != 200:
                        raise CIError(
                            f"GitHub refused the artifact download: HTTP {response.status_code}"
                        )
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > MAX_ARTIFACT_BYTES:
                        raise CIError("The test artifact is too large")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_ARTIFACT_BYTES:
                            raise CIError("The test artifact is too large")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as exc:
                raise CIError(f"The artifact download failed ({type(exc).__name__})") from None
        raise CIError("The artifact download was redirected too often")


# ---- the provider ------------------------------------------------------------------------------


class CIExecutionProvider:
    """ExecutionProvider running allowlisted commands in GitHub Actions. Unbound it only reports
    "unavailable"; the test runner binds it to a run (project id, run id) before use."""

    name = PROVIDER_NAME
    supports_baseline = False  # the base branch is the baseline; no second workflow run
    supports_rerun = False  # a lone re-run would be another full workflow run

    def __init__(
        self,
        targets: TargetLoader,
        workspace: WorkspaceLoader,
        *,
        client: httpx.Client | None = None,
        workflow_file: str = WORKFLOW_FILE,
        poll_interval: float = 10.0,
        approval_timeout: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        project_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> None:
        self._targets = targets
        self._workspace = workspace
        self._client = client
        self._workflow = workflow_file
        self._poll = poll_interval
        self._approval_timeout = approval_timeout
        self._clock = clock
        self._wall = wall
        self._project_id = project_id
        self._run_id = run_id

    def bind(self, project_id: uuid.UUID, run_id: uuid.UUID) -> "CIExecutionProvider":
        return CIExecutionProvider(
            self._targets,
            self._workspace,
            client=self._client,
            workflow_file=self._workflow,
            poll_interval=self._poll,
            approval_timeout=self._approval_timeout,
            clock=self._clock,
            wall=self._wall,
            project_id=project_id,
            run_id=run_id,
        )

    def release(self, run_id: uuid.UUID) -> None:
        release_state(run_id)

    def status(self) -> tuple[bool, str]:
        if self._project_id is None:
            return False, "The CI provider is not bound to a run"
        try:
            self._targets(self._project_id)
        except ProviderUnavailableError as exc:
            return False, str(exc)
        return True, ""

    def cancel(self, job_id: str) -> None:
        if self._project_id is None or self._run_id is None:
            return
        with _cond:
            _state(self._project_id, self._run_id).cancelled_jobs.add(job_id)
            _cond.notify_all()

    # ---- one job -------------------------------------------------------------------------------

    def run(
        self,
        command_id: str,
        files: Mapping[str, str],
        timeout: float,
        output_cap: int,
        *,
        job_id: str = "",
        select: str | None = None,
    ) -> ExecutionResult:
        check_command(command_id)
        if select:
            raise UnknownCommandError("The CI provider does not run single tests")
        if self._project_id is None or self._run_id is None:
            raise ProviderUnavailableError("The CI provider is not bound to a run")
        target = self._targets(self._project_id)  # raises ProviderUnavailableError
        state = _state(self._project_id, self._run_id)
        started = self._clock()
        meta: dict[str, Any] = {"provider": PROVIDER_NAME, "workflow": self._workflow}
        with _cond:
            state.command_id = command_id
            state.workflow_run_id = state.workflow_url = state.conclusion = None
            state.message = None
        outcome = self._job(target, state, meta, command_id, files, timeout, job_id)
        with _cond:
            meta.update(approved_by=state.approved_by)
            meta.setdefault("kept", False)
        stderr = _scrub(str(outcome.pop("stderr", "")), target.token)
        stdout = _scrub(str(outcome.pop("stdout", "")), target.token)
        return make_result(
            stdout=stdout,
            stderr=stderr,
            duration=self._clock() - started,
            output_cap=output_cap,
            meta={"ci": dict(meta)},
            **outcome,
        )

    def _job(
        self,
        target: CITarget,
        state: _RunState,
        meta: dict[str, Any],
        command_id: str,
        files: Mapping[str, str],
        timeout: float,
        job_id: str,
    ) -> dict[str, Any]:
        """The job's outcome as make_result arguments; `meta` collects what is stored."""
        assert self._run_id is not None
        started = self._clock()

        def cancelled() -> bool:
            with _cond:
                return job_id in state.cancelled_jobs

        try:
            branch = ci_branch(target.branch_prefix, self._run_id)
            meta["branch"] = branch
            check_ci_branch(
                branch,
                prefix=target.branch_prefix,
                base_branch=target.base_branch,
                default_branch=target.base_branch,
            )
            upserts, deletions = self._changes(files)
        except CIError as exc:
            return self._failed(state, meta, str(exc))

        try:
            decision = self._await_approval(state, cancelled, started + self._approval_timeout)
        except _Cancelled:
            return self._ended(state, meta, "cancelled")
        except _TimedOut:
            meta["status"] = "approval_timed_out"
            self._phase(state, "approval_timed_out")
            return {
                "exit_code": None,
                "timed_out": True,
                "stderr": "No answer to the CI push approval",
            }
        if decision != "approved":
            meta["status"] = "denied"
            self._phase(state, "denied")
            return {"exit_code": None, "cancelled": True, "stderr": DENIED_MESSAGE}

        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False)
        api = _Api(
            client,
            target,
            deadline=self._clock() + timeout,
            clock=self._clock,
            wall=self._wall,
            cancelled=cancelled,
        )
        pushed = False
        try:
            self._phase(state, "pushing", branch=branch)
            repo = api.json(api.call("GET", "", "the repository read"))
            check_ci_branch(
                branch,
                prefix=target.branch_prefix,
                base_branch=target.base_branch,
                default_branch=str(repo.get("default_branch") or target.base_branch),
            )
            commit = self._push(api, target, branch, upserts, deletions, job_id)
            pushed = True
            meta["commit"] = commit
            self._dispatch(api, branch, command_id)
            self._phase(state, "queued")
            run_number = self._find_run(api, branch, commit)
            url = f"{target.html_repo}/actions/runs/{run_number}"
            meta.update(workflow_run_id=run_number, workflow_url=url)
            self._phase(state, "queued", workflow_run_id=run_number, workflow_url=url)
            logger.info(
                "ci dispatched run=%s branch=%s workflow_run=%s", self._run_id, branch, run_number
            )
            conclusion = self._wait(api, state, run_number)
            meta["conclusion"] = conclusion
            self._phase(state, "downloading", conclusion=conclusion)
            if conclusion in ("cancelled", "timed_out"):
                return self._ended(state, meta, conclusion)
            report = self._artifact(api, run_number)
            meta.update(status="completed", report=report is not None)
            self._phase(state, "completed")
            stdout = f"GitHub Actions {self._workflow} on {branch}: {conclusion}\n{url}"
            if report is None:
                stdout += f"\nNo {ARTIFACT_NAME} artifact with a JUnit report was found."
            return {
                "exit_code": 0 if conclusion == "success" else 1,
                "stdout": stdout,
                "report": report,
            }
        except _Cancelled:
            self._cancel_remote(api, meta)
            return self._ended(state, meta, "cancelled")
        except _TimedOut:
            self._cancel_remote(api, meta)
            return self._ended(state, meta, "timed_out")
        except CIError as exc:
            return self._failed(state, meta, _scrub(str(exc), target.token))
        finally:
            if pushed:
                self._cleanup(api, state, branch, meta)
            if self._client is None:
                client.close()

    # ---- steps ---------------------------------------------------------------------------------

    def _phase(self, state: _RunState, phase: str, **extra: Any) -> None:
        with _cond:
            state.phase = phase
            for key, value in extra.items():
                setattr(state, key, value)

    def _failed(self, state: _RunState, meta: dict[str, Any], message: str) -> dict[str, Any]:
        meta.update(status="error", error=message)
        self._phase(state, "failed", message=message)
        logger.info("ci job failed run=%s", self._run_id)
        return {"exit_code": None, "stderr": f"CI: {message}"}

    def _ended(self, state: _RunState, meta: dict[str, Any], how: str) -> dict[str, Any]:
        meta["status"] = how
        self._phase(state, how)
        if how == "cancelled":
            return {"exit_code": None, "cancelled": True, "stderr": "CI job cancelled"}
        return {"exit_code": None, "timed_out": True, "stderr": "CI job timed out"}

    def _changes(self, files: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
        """What the draft changes against the accepted workspace (the patch to apply on the base
        branch). Workflows and other CI configuration are never pushed."""
        assert self._project_id is not None
        accepted = self._workspace(self._project_id)
        upserts = {p: c for p, c in files.items() if accepted.get(p) != c}
        deletions = sorted(p for p in accepted if p not in files)
        if len(upserts) + len(deletions) > MAX_COMMIT_FILES:
            raise CIError("Refused: the draft changes too many files for one CI commit")
        for path in [*upserts, *deletions]:
            if path.replace("\\", "/").lower().startswith(".github/"):
                raise CIError("Refused: the draft changes files under .github/")
            if path_problem(path) is not None:
                raise CIError("Refused: the draft holds a path that cannot be pushed")
        return upserts, deletions

    def _await_approval(
        self, state: _RunState, cancelled: Callable[[], bool], deadline: float
    ) -> str:
        with _cond:
            if state.approval in ("approved", "denied"):
                return state.approval
            state.approval, state.phase = "pending", "awaiting_approval"
            _cond.notify_all()
            while True:
                if cancelled():
                    state.approval = "none"
                    raise _Cancelled
                if state.approval != "pending":
                    return state.approval
                remaining = deadline - self._clock()
                if remaining <= 0:
                    state.approval = "none"
                    raise _TimedOut
                _cond.wait(min(remaining, 0.5))

    def _push(
        self,
        api: _Api,
        target: CITarget,
        branch: str,
        upserts: Mapping[str, str],
        deletions: list[str],
        job_id: str,
    ) -> str:
        base_ref = quote(target.base_branch, safe="/")
        head = api.json(api.call("GET", f"git/ref/heads/{base_ref}", "the base branch read"))
        base_sha = str((head.get("object") or {}).get("sha") or "")
        commit = api.json(api.call("GET", f"git/commits/{base_sha}", "the base commit read"))
        base_tree = str((commit.get("tree") or {}).get("sha") or "")
        listing = api.json(
            api.call(
                "GET", f"git/trees/{base_tree}", "the base tree read", params={"recursive": "1"}
            )
        )
        known = {
            str(e.get("path")): e
            for e in listing.get("tree") or []
            if isinstance(e, dict) and e.get("type") == "blob"
        }
        entries: list[dict[str, Any]] = []
        for path, content in sorted(upserts.items()):
            existing = known.get(path)
            if existing is not None and existing.get("sha") == git_blob_sha(content):
                continue
            mode = str(existing.get("mode")) if existing is not None else "100644"
            entries.append({"path": path, "mode": mode, "type": "blob", "content": content})
        for path in deletions:
            if path in known:
                entries.append(
                    {
                        "path": path,
                        "mode": str(known[path].get("mode")),
                        "type": "blob",
                        "sha": None,
                    }
                )
        tree_sha = base_tree
        if entries:
            created = api.call(
                "POST",
                "git/trees",
                "the tree creation",
                ok=(201,),
                json={"base_tree": base_tree, "tree": entries},
            )
            tree_sha = str(api.json(created).get("sha") or "")

        ref_path = f"git/ref/heads/{quote(branch, safe='/')}"
        existing_ref = api.call("GET", ref_path, "the CI branch read", ok=(200, 404))
        parent = base_sha
        if existing_ref.status_code == 200:
            old_sha = str((api.json(existing_ref).get("object") or {}).get("sha") or "")
            old = api.json(api.call("GET", f"git/commits/{old_sha}", "the CI branch read"))
            if f"{RUN_TRAILER}: {self._run_id}" not in str(old.get("message") or ""):
                raise CIError("Refused: the CI branch exists and was not created by this run")
            parent = old_sha
        message = (
            f"ArchPilotAI CI run\n\n{PROJECT_TRAILER}: {self._project_id}\n"
            f"{RUN_TRAILER}: {self._run_id}\n{JOB_TRAILER}: {job_id}\n"
        )
        made = api.call(
            "POST",
            "git/commits",
            "the commit creation",
            ok=(201,),
            json={"message": message, "tree": tree_sha, "parents": [parent]},
        )
        new_sha = str(api.json(made).get("sha") or "")
        if existing_ref.status_code == 200:
            api.call(
                "PATCH",
                f"git/refs/heads/{quote(branch, safe='/')}",
                "the CI branch update",
                json={"sha": new_sha, "force": False},
            )
        else:
            api.call(
                "POST",
                "git/refs",
                "the CI branch creation",
                ok=(201,),
                json={"ref": f"refs/heads/{branch}", "sha": new_sha},
            )
        logger.info(
            "ci branch pushed run=%s branch=%s files=%d", self._run_id, branch, len(entries)
        )
        return new_sha

    def _dispatch(self, api: _Api, branch: str, command_id: str) -> None:
        api.call(
            "POST",
            f"actions/workflows/{quote(self._workflow, safe='')}/dispatches",
            "the workflow dispatch",
            ok=(200, 204),
            json={
                "ref": branch,
                "inputs": {"command": command_id, "archpilot_run": str(self._run_id)},
            },
        )

    def _find_run(self, api: _Api, branch: str, commit: str) -> int:
        """The workflow run started by the dispatch: on the CI branch, at the pushed commit."""
        while True:
            found = api.json(
                api.call(
                    "GET",
                    f"actions/workflows/{quote(self._workflow, safe='')}/runs",
                    "the workflow run lookup",
                    params={
                        "branch": branch,
                        "event": "workflow_dispatch",
                        "head_sha": commit,
                        "per_page": "10",
                    },
                )
            )
            for item in found.get("workflow_runs") or []:
                if isinstance(item, dict) and item.get("head_sha") == commit:
                    number = item.get("id")
                    if isinstance(number, int):
                        return number
            api.sleep(self._poll)

    def _wait(self, api: _Api, state: _RunState, run_number: int) -> str:
        while True:
            data = api.json(api.call("GET", f"actions/runs/{run_number}", "the workflow status"))
            status = str(data.get("status") or "")
            if status == "completed":
                return str(data.get("conclusion") or "unknown")
            self._phase(state, "in_progress" if status == "in_progress" else "queued")
            api.sleep(self._poll)

    def _artifact(self, api: _Api, run_number: int) -> str | None:
        listing = api.json(
            api.call(
                "GET",
                f"actions/runs/{run_number}/artifacts",
                "the artifact list",
                params={"name": ARTIFACT_NAME},
            )
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

    def _cancel_remote(self, api: _Api, meta: dict[str, Any]) -> None:
        number = meta.get("workflow_run_id")
        if not isinstance(number, int):
            return
        try:
            api.call(
                "POST",
                f"actions/runs/{number}/cancel",
                "the workflow cancel",
                ok=(202, 409),
                bounded=False,
            )
            meta["remote_cancelled"] = True
        except CIError:
            meta["remote_cancelled"] = False

    def _cleanup(self, api: _Api, state: _RunState, branch: str, meta: dict[str, Any]) -> None:
        with _cond:
            keep = state.keep_branches
        meta["kept"] = keep
        if keep:
            meta["branch_deleted"] = False
            return
        try:
            api.call(
                "DELETE",
                f"git/refs/heads/{quote(branch, safe='/')}",
                "the CI branch deletion",
                ok=(204, 404, 422),
                bounded=False,
            )
            meta["branch_deleted"] = True
        except CIError:
            meta["branch_deleted"] = False
            logger.info("ci branch not deleted run=%s", self._run_id)


# ---- wiring ------------------------------------------------------------------------------------


def settings_targets(session: Session, store: CredentialStore) -> TargetLoader:
    """Reads the project's Git settings and its stored token for each job."""

    def load(project_id: uuid.UUID) -> CITarget:
        row = session.get(GitRemoteSettings, project_id)
        if row is None:
            raise ProviderUnavailableError("No Git repository is configured for this project")
        if row.provider not in SUPPORTED_PROVIDERS:
            raise ProviderUnavailableError("CI runs need a GitHub or GitHub Enterprise repository")
        if not row.pat_ref or row.pat_provider != row.provider:
            raise ProviderUnavailableError("No personal access token is saved for the repository")
        try:
            token = store.get(row.pat_ref)
        except CredentialStoreUnavailableError:
            raise ProviderUnavailableError("The credential store is unavailable") from None
        if not token:
            raise ProviderUnavailableError("The saved personal access token was not found")
        try:
            remote = parse_remote(row.remote_url)
        except GitSettingsError:
            raise ProviderUnavailableError("The remote URL is not valid") from None
        if len(remote.segments) != 2:
            raise ProviderUnavailableError("The remote URL is not a GitHub repository")
        return CITarget(row.provider, remote, row.base_branch, row.branch_prefix, token)

    return load
