"""Git operations for a project's external repository.

Safety rules kept here: the personal access token reaches git only through the environment of one
child process (a temporary credential helper), never inside a URL or an argument, and never in a
log or an error message. Push is a separate, explicit method that only an HTTP endpoint calls; it
is not a tool and no agent code imports this module.
"""

import logging
import os
import re
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.domain.git_remote import (
    GitSettingsError,
    RemoteRef,
    branch_name,
    compare_url,
    is_own_repository,
    parse_remote,
    validate_auth,
    validate_branch_name,
    validate_prefix,
    validate_provider,
)
from app.domain.patch import path_problem
from app.models.git_remote import GitRemoteSettings
from app.models.patch import CodePatch, PatchFile
from app.models.plan import PlanTask
from app.models.project import Project
from app.repositories.patch import PatchRepository
from app.services.credential_store import CredentialStore, CredentialStoreUnavailableError
from app.services.git_provider import (
    ProviderApiError,
    ProviderClient,
    PullRequestInput,
    api_supported,
)
from app.services.patch import PatchNotFoundError
from app.services.project import ProjectNotFoundError
from app.services.workspace_export import REPO_ROOT

logger = logging.getLogger(__name__)

TRAILER = "ArchPilot-Project"
PROTECTED_NAMES = frozenset({"main", "master"})
GIT_TIMEOUT_SECONDS = 120.0
_FOLDER_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]*$")
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_URL_CREDENTIALS = re.compile(r"(https?://)[^/\s@]+@")
_PAT_HELPER = '!f() { echo username=archpilot; echo "password=$ARCHPILOT_GIT_PAT"; }; f'


class GitRemoteError(Exception):
    """A refusal or failure; `code` is a fixed label and the message never holds a secret."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SettingsNotFoundError(GitRemoteError):
    def __init__(self) -> None:
        super().__init__("not_configured", "No Git repository is configured for this project")


def redact(text: str, token: str | None = None) -> str:
    if token:
        text = text.replace(token, "***")
    return _URL_CREDENTIALS.sub(r"\1***@", text)


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str
    err: str


class GitRunner:
    """Runs git as a child process. The token, if any, is passed through the environment only."""

    def __init__(self, timeout: float = GIT_TIMEOUT_SECONDS, allow_local: bool = False) -> None:
        self._timeout = timeout
        self._allow_local = allow_local

    def _env(self, token: str | None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in {"GIT_DIR", "GIT_WORK_TREE"}}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ALLOW_PROTOCOL"] = "https:ssh:file" if self._allow_local else "https:ssh"
        env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
        if token:
            env["ARCHPILOT_GIT_PAT"] = token
            env["GIT_CONFIG_COUNT"] = "2"
            env["GIT_CONFIG_KEY_0"] = "credential.helper"
            env["GIT_CONFIG_VALUE_0"] = ""
            env["GIT_CONFIG_KEY_1"] = "credential.helper"
            env["GIT_CONFIG_VALUE_1"] = _PAT_HELPER
        return env

    def run(
        self, args: Sequence[str], cwd: Path | None = None, token: str | None = None
    ) -> GitResult:
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=self._env(token),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise GitRemoteError("git_failed", "git is not installed or not on PATH") from None
        except subprocess.TimeoutExpired:
            raise GitRemoteError("git_failed", "git timed out") from None
        return GitResult(done.returncode, done.stdout, redact(done.stderr, token))


@dataclass(frozen=True)
class GitSettingsInput:
    provider: str
    remote_url: str
    clone_folder: str | None
    base_branch: str
    branch_prefix: str
    author_name: str
    author_email: str
    auth_method: str


@dataclass(frozen=True)
class ConnectionResult:
    reachable: bool
    api_checked: bool
    api_ok: bool | None
    message: str


@dataclass(frozen=True)
class CommitResult:
    branch: str
    commit: str
    created_branch: bool
    files: list[str]


@dataclass(frozen=True)
class PullRequestOutcome:
    mode: str  # "created" | "compare_link"
    url: str | None
    branch: str
    base: str
    title: str
    message: str


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _check_text(value: str, what: str, limit: int) -> str:
    text = value.strip()
    if not text or len(text) > limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise GitSettingsError(f"The {what} is empty, too long or has control characters")
    return text


def _own_remotes() -> list[RemoteRef]:
    result = GitRunner(timeout=10).run(["config", "--get-regexp", r"^remote\..*\.url$"], REPO_ROOT)
    refs: list[RemoteRef] = []
    for line in result.out.splitlines():
        try:
            refs.append(parse_remote(line.split(None, 1)[1]))
        except (IndexError, GitSettingsError):
            continue
    return refs


class GitRemoteService:
    def __init__(
        self,
        session: Session,
        *,
        workspace_root: str,
        store: CredentialStore,
        runner: GitRunner | None = None,
        provider_client: ProviderClient | None = None,
        allow_local: bool = False,
        own_remotes: Sequence[RemoteRef] | None = None,
    ) -> None:
        self._session = session
        self._root_setting = workspace_root
        self._store = store
        self._git = runner or GitRunner(allow_local=allow_local)
        self._provider = provider_client or ProviderClient()
        self._allow_local = allow_local
        self._own = list(own_remotes) if own_remotes is not None else None
        self._patches = PatchRepository(session)

    # ---- settings --------------------------------------------------------------------------

    def get(self, project_id: uuid.UUID) -> GitRemoteSettings:
        self._project(project_id)
        row = self._session.get(GitRemoteSettings, project_id)
        if row is None:
            raise SettingsNotFoundError
        return row

    def save(self, project_id: uuid.UUID, data: GitSettingsInput) -> GitRemoteSettings:
        self._project(project_id)
        remote = parse_remote(data.remote_url, allow_local=self._allow_local)
        validate_provider(data.provider, remote)
        validate_auth(data.auth_method, remote)
        if is_own_repository(remote, self._own_repos()):
            raise GitRemoteError(
                "own_repository", "The ArchPilotAI repository itself cannot be a target"
            )
        base = validate_branch_name(data.base_branch, "base branch")
        prefix = validate_prefix(data.branch_prefix)
        name = _check_text(data.author_name, "author name", 100)
        email = _check_text(data.author_email, "author email", 200)
        if not _EMAIL.match(email):
            raise GitSettingsError("The author email is not valid")
        folder = self._clone_folder(project_id, data.clone_folder)

        row = self._session.get(GitRemoteSettings, project_id)
        if row is None:
            row = GitRemoteSettings(project_id=project_id)
            self._session.add(row)
        elif row.remote_url.strip() != data.remote_url.strip():
            self._drop_pat(row)  # the token is keyed by project and remote
        row.provider = data.provider
        row.remote_url = data.remote_url.strip()
        row.clone_folder = folder
        row.base_branch = base
        row.branch_prefix = prefix
        row.author_name = name
        row.author_email = email
        row.auth_method = data.auth_method
        self._session.commit()
        self._session.refresh(row)
        return row

    def delete(self, project_id: uuid.UUID) -> None:
        row = self.get(project_id)
        self._drop_pat(row)
        self._session.delete(row)
        self._session.commit()

    def _own_repos(self) -> list[RemoteRef]:
        if self._own is None:
            self._own = _own_remotes()
        return self._own

    def _project(self, project_id: uuid.UUID) -> None:
        if self._session.get(Project, project_id) is None:
            raise ProjectNotFoundError(project_id)

    def _root(self) -> Path:
        if not self._root_setting.strip():
            raise GitRemoteError(
                "workspace_not_configured",
                "ARCHPILOT_WORKSPACE_ROOT is not configured; set it to a local folder",
            )
        root = Path(self._root_setting.strip()).expanduser()
        if not root.is_absolute():
            raise GitRemoteError(
                "workspace_location", "ARCHPILOT_WORKSPACE_ROOT must be an absolute path"
            )
        resolved = root.resolve()
        if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
            raise GitRemoteError(
                "workspace_location",
                "ARCHPILOT_WORKSPACE_ROOT must be outside the ArchPilotAI repository",
            )
        return resolved

    def _clone_folder(self, project_id: uuid.UUID, requested: str | None) -> str:
        self._root()
        folder = (requested or "").strip().replace("\\", "/").rstrip("/") or f"git/{project_id}"
        if not all(_FOLDER_PART.match(p) and not p.endswith(".") for p in folder.split("/")):
            raise GitSettingsError("The clone folder must be a relative folder name")
        parts = folder.split("/")
        if parts[0] == ".git" or any(p.lower() == ".git" for p in parts):
            raise GitSettingsError("The clone folder must not be inside a .git folder")
        if _is_uuid(parts[0]):
            raise GitSettingsError("The clone folder is reserved for the workspace export")
        others = self._session.query(GitRemoteSettings).filter(
            GitRemoteSettings.project_id != project_id
        )
        for other in others:
            a, b = folder.lower().split("/"), other.clone_folder.lower().split("/")
            if a[: len(b)] == b or b[: len(a)] == a:
                raise GitRemoteError(
                    "clone_folder_taken", "The clone folder is used by another project"
                )
        return folder

    def clone_path(self, settings: GitRemoteSettings) -> Path:
        root = self._root()
        path = (root / settings.clone_folder).resolve()
        if root not in path.parents:
            raise GitRemoteError(
                "workspace_location", "The clone folder is outside the workspace root"
            )
        if path == REPO_ROOT or REPO_ROOT in path.parents:
            raise GitRemoteError("own_repository", "The clone folder is inside ArchPilotAI")
        return path

    def is_cloned(self, settings: GitRemoteSettings) -> bool:
        try:
            return (self.clone_path(settings) / ".git").exists()
        except GitRemoteError:
            return False

    # ---- personal access token ---------------------------------------------------------------

    def save_pat(self, project_id: uuid.UUID, token: str) -> GitRemoteSettings:
        row = self.get(project_id)
        secret = token.strip()
        if not 8 <= len(secret) <= 500 or any(c.isspace() or ord(c) < 32 for c in secret):
            raise GitSettingsError("The access token is not valid")
        if not self._store.available():
            raise CredentialStoreUnavailableError
        remote = parse_remote(row.remote_url, allow_local=self._allow_local)
        reference = f"{project_id}|{remote.normalized}"
        if row.pat_ref and row.pat_ref != reference:
            self._store.delete(row.pat_ref)
        self._store.set(reference, secret)
        row.pat_ref = reference
        row.pat_provider = row.provider
        row.pat_last4 = secret[-4:]
        self._session.commit()
        self._session.refresh(row)
        return row

    def delete_pat(self, project_id: uuid.UUID) -> GitRemoteSettings:
        row = self.get(project_id)
        self._drop_pat(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def _drop_pat(self, row: GitRemoteSettings) -> None:
        if row.pat_ref:
            self._store.delete(row.pat_ref)
        row.pat_ref = row.pat_provider = row.pat_last4 = None

    def _stored_pat(self, row: GitRemoteSettings) -> str | None:
        if not row.pat_ref:
            return None
        return self._store.get(row.pat_ref)

    def _git_token(self, row: GitRemoteSettings) -> str | None:
        """The token git may use: only when the auth method is "pat"."""
        if row.auth_method != "pat":
            return None
        token = self._stored_pat(row)
        if token is None:
            raise GitRemoteError(
                "pat_missing", "The access token is not in the credential store; save it again"
            )
        return token

    # ---- test, clone, attach -------------------------------------------------------------------

    def test_connection(self, project_id: uuid.UUID) -> ConnectionResult:
        row = self.get(project_id)
        remote = self._remote(row)
        token = self._git_token(row)
        listing = self._git.run(["ls-remote", "--heads", "--", row.remote_url], None, token)
        if listing.code != 0:
            detail = listing.err.strip()[:300] or "git ls-remote failed"
            return ConnectionResult(False, False, None, f"The remote is not reachable: {detail}")
        api_token = self._api_token(row)
        if api_token is None or not api_supported(row.provider) or remote.scheme == "local":
            return ConnectionResult(True, False, None, "The remote is reachable")
        try:
            self._provider.check_read(row.provider, remote, api_token)
        except ProviderApiError as exc:
            return ConnectionResult(True, True, False, str(exc))
        return ConnectionResult(True, True, True, "The remote and the provider API are reachable")

    def _api_token(self, row: GitRemoteSettings) -> str | None:
        try:
            return self._stored_pat(row)
        except CredentialStoreUnavailableError:
            return None

    def clone(self, project_id: uuid.UUID) -> Path:
        row = self.get(project_id)
        path = self.clone_path(row)
        if (path / ".git").exists():
            raise GitRemoteError("already_cloned", "The clone folder already holds a repository")
        if path.exists() and any(path.iterdir()):
            raise GitRemoteError("folder_not_empty", "The clone folder exists and is not empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        token = self._git_token(row)
        result = self._git.run(
            ["clone", "--branch", row.base_branch, "--", row.remote_url, str(path)], None, token
        )
        if result.code != 0:
            raise GitRemoteError("git_failed", f"git clone failed: {result.err.strip()[:300]}")
        logger.info("git clone finished project=%s", project_id)
        return path

    def attach(self, project_id: uuid.UUID) -> Path:
        row = self.get(project_id)
        path = self.clone_path(row)
        if not (path / ".git").exists():
            raise GitRemoteError("not_cloned", "No local clone exists in the clone folder")
        top = self._git.run(["rev-parse", "--show-toplevel"], path)
        if top.code != 0 or Path(top.out.strip()).resolve() != path:
            raise GitRemoteError("not_cloned", "The clone folder is not the root of a repository")
        origin = self._git.run(["remote", "get-url", "origin"], path)
        if origin.code != 0:
            raise GitRemoteError("remote_mismatch", "The local clone has no origin remote")
        try:
            actual = parse_remote(origin.out.strip(), allow_local=self._allow_local)
        except GitSettingsError:
            raise GitRemoteError(
                "remote_mismatch", "The origin of the local clone is not valid"
            ) from None
        if is_own_repository(actual, self._own_repos()):
            raise GitRemoteError(
                "own_repository", "The ArchPilotAI repository itself cannot be a target"
            )
        if actual.normalized != self._remote(row).normalized:
            raise GitRemoteError(
                "remote_mismatch",
                "The origin of the local clone differs from the configured remote",
            )
        return path

    def _remote(self, row: GitRemoteSettings) -> RemoteRef:
        return parse_remote(row.remote_url, allow_local=self._allow_local)

    # ---- commit --------------------------------------------------------------------------------

    def branch_for(self, row: GitRemoteSettings, patch: CodePatch, title: str) -> str:
        return branch_name(row.branch_prefix, patch.task_key, title)

    def _patch(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> tuple[CodePatch, str]:
        patch = self._patches.get(project_id, patch_id)
        if patch is None:
            raise PatchNotFoundError(patch_id)
        task = self._session.get(PlanTask, patch.task_id)
        return patch, task.title if task is not None else patch.task_key

    def commit_patch(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> CommitResult:
        row = self.get(project_id)
        patch, title = self._patch(project_id, patch_id)
        if patch.status != "accepted":
            raise GitRemoteError("patch_not_accepted", "Only an accepted patch can be committed")
        clone = self.attach(project_id)
        branch = validate_branch_name(self.branch_for(row, patch, title))
        if branch == row.base_branch or branch.lower() in PROTECTED_NAMES:
            raise GitRemoteError("protected_branch", "The work branch would be the base branch")
        files = list(self._patches.files(project_id, patch_id))
        targets = self._targets(clone, files)

        if self._git.run(["status", "--porcelain"], clone).out.strip():
            raise GitRemoteError(
                "dirty_tree", "The working tree has uncommitted changes; clean it first"
            )
        original = self._git.run(["rev-parse", "--abbrev-ref", "HEAD"], clone).out.strip()
        base_ref = self._first_ref(
            clone, [f"refs/heads/{row.base_branch}", f"refs/remotes/origin/{row.base_branch}"]
        )
        if base_ref is None:
            raise GitRemoteError("base_missing", "The base branch does not exist in the clone")

        local = self._ref_exists(clone, f"refs/heads/{branch}")
        remote_ref = f"refs/remotes/origin/{branch}"
        remote_has = self._ref_exists(clone, remote_ref)
        for ref in (f"refs/heads/{branch}" if local else None, remote_ref if remote_has else None):
            if ref and self._foreign_commits(clone, base_ref, ref, project_id):
                raise GitRemoteError(
                    "foreign_commits", "The branch holds commits not made by ArchPilotAI"
                )

        created = not local
        try:
            if local:
                self._must(["checkout", "-q", branch, "--"], clone)
            elif remote_has:
                self._must(["checkout", "-q", "-b", branch, remote_ref], clone)
            else:
                self._must(["checkout", "-q", "-b", branch, base_ref], clone)
            self._apply(clone, files, targets)
            self._must(["add", "-A", "--", *targets.keys()], clone)
            if self._git.run(["diff", "--cached", "--quiet"], clone).code == 0:
                raise GitRemoteError(
                    "nothing_to_commit",
                    "The patch changes nothing on the branch (already committed?)",
                )
            self._must(
                [
                    "-c", f"user.name={row.author_name}",
                    "-c", f"user.email={row.author_email}",
                    "-c", "commit.gpgsign=false",
                    "commit", "-q", "--no-verify", "-m",
                    self._message(patch, title, project_id),
                ],
                clone,
            )  # fmt: skip
        except BaseException:
            self._rollback(clone, original, branch if created else None, list(targets))
            raise
        sha = self._must(["rev-parse", "HEAD"], clone).strip()
        logger.info("patch committed project=%s branch=%s", project_id, branch)
        return CommitResult(branch, sha, created, sorted(targets))

    @staticmethod
    def _message(patch: CodePatch, title: str, project_id: uuid.UUID) -> str:
        reviewer = patch.reviewed_by or "unknown"
        return (
            f"{patch.task_key}: {title[:80]}\n\n"
            f"Task: {patch.task_key}\n"
            f"Patch: v{patch.version} ({patch.id})\n"
            f"Reviewer: {reviewer}\n"
            f"{TRAILER}: {project_id}\n"
        )

    def _targets(self, clone: Path, files: Sequence[PatchFile]) -> dict[str, Path]:
        targets: dict[str, Path] = {}
        for item in files:
            parts = item.path.replace("\\", "/").split("/")
            if path_problem(item.path) is not None or any(p.lower() == ".git" for p in parts):
                raise GitRemoteError("invalid_path", "The patch holds an unacceptable file path")
            target = (clone / Path(*parts)).resolve()
            if clone not in target.parents:
                raise GitRemoteError("invalid_path", "The patch holds an unacceptable file path")
            targets["/".join(parts)] = target
        return targets

    def _apply(self, clone: Path, files: Sequence[PatchFile], targets: dict[str, Path]) -> None:
        for item in files:
            target = targets["/".join(item.path.replace("\\", "/").split("/"))]
            if item.change == "delete":
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item.content.encode("utf-8"))

    def _rollback(self, clone: Path, original: str, created: str | None, paths: list[str]) -> None:
        self._git.run(["reset", "-q", "--hard"], clone)
        if paths:
            self._git.run(["clean", "-fdq", "--", *paths], clone)
        if original and original != "HEAD":
            self._git.run(["checkout", "-q", original, "--"], clone)
        if created:
            self._git.run(["branch", "-D", created], clone)

    def _must(self, args: Sequence[str], cwd: Path, token: str | None = None) -> str:
        result = self._git.run(args, cwd, token)
        if result.code != 0:
            raise GitRemoteError("git_failed", f"git {args[0]} failed: {result.err.strip()[:300]}")
        return result.out

    def _ref_exists(self, clone: Path, ref: str) -> bool:
        return self._git.run(["rev-parse", "--verify", "--quiet", ref], clone).code == 0

    def _first_ref(self, clone: Path, refs: Sequence[str]) -> str | None:
        return next((r for r in refs if self._ref_exists(clone, r)), None)

    def _foreign_commits(self, clone: Path, base_ref: str, ref: str, project_id: uuid.UUID) -> int:
        log = self._must(["log", "--format=%B%x1e", f"{base_ref}..{ref}"], clone)
        marker = f"{TRAILER}: {project_id}"
        messages = [m for m in log.split("\x1e") if m.strip()]
        return sum(1 for m in messages if marker not in m.splitlines())

    # ---- push (explicit user action only) -----------------------------------------------------

    def push(self, project_id: uuid.UUID, branch: str, approved_by: str) -> str:
        """Push one work branch. Never the base or default branch, never forced."""
        row = self.get(project_id)
        approver = approved_by.strip()
        if not approver or len(approver) > 100:
            raise GitSettingsError("A person must approve the push by name")
        name = validate_branch_name(branch)
        clone = self.attach(project_id)
        default = self._git.run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], clone)
        default_name = default.out.strip().removeprefix("origin/") if default.code == 0 else ""
        protected = {row.base_branch.lower(), default_name.lower()} | PROTECTED_NAMES
        if name.lower() in protected:
            raise GitRemoteError(
                "protected_branch", "Pushing to the base or default branch is not allowed"
            )
        if row.branch_prefix and not name.startswith(row.branch_prefix):
            raise GitRemoteError(
                "protected_branch", "Only branches with the configured prefix can be pushed"
            )
        if not self._ref_exists(clone, f"refs/heads/{name}"):
            raise GitRemoteError("branch_not_found", "The branch does not exist in the clone")
        base_ref = self._first_ref(
            clone, [f"refs/heads/{row.base_branch}", f"refs/remotes/origin/{row.base_branch}"]
        )
        if base_ref is None:
            raise GitRemoteError("base_missing", "The base branch does not exist in the clone")
        if self._foreign_commits(clone, base_ref, f"refs/heads/{name}", project_id):
            raise GitRemoteError(
                "foreign_commits", "The branch holds commits not made by ArchPilotAI"
            )
        ahead = self._must(["rev-list", "--count", f"{base_ref}..refs/heads/{name}"], clone)
        if int(ahead.strip() or "0") == 0:
            raise GitRemoteError("nothing_to_commit", "The branch has no commits of its own")
        token = self._git_token(row)
        self._must(
            [
                "-c", "push.default=nothing", "push", "--porcelain", "origin",
                f"refs/heads/{name}:refs/heads/{name}",
            ],
            clone,
            token,
        )  # fmt: skip
        logger.info("branch pushed project=%s branch=%s approved_by=%s", project_id, name, approver)
        return name

    # ---- pull request ---------------------------------------------------------------------------

    def pull_request(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> PullRequestOutcome:
        """Open a pull request through the provider API (with a token) or return a compare link.
        Never merges."""
        row = self.get(project_id)
        patch, title = self._patch(project_id, patch_id)
        if patch.status != "accepted":
            raise GitRemoteError("patch_not_accepted", "Only an accepted patch has a pull request")
        clone = self.attach(project_id)
        head = validate_branch_name(self.branch_for(row, patch, title))
        if head == row.base_branch or head.lower() in PROTECTED_NAMES:
            raise GitRemoteError("protected_branch", "The work branch would be the base branch")
        if not self._ref_exists(clone, f"refs/heads/{head}"):
            raise GitRemoteError("branch_not_found", "Commit the patch to its branch first")
        listing = self._git.run(
            ["ls-remote", "--heads", "origin", f"refs/heads/{head}"], clone, self._git_token(row)
        )
        if listing.code != 0:
            raise GitRemoteError("git_failed", f"git ls-remote failed: {listing.err.strip()[:300]}")
        if not listing.out.strip():
            raise GitRemoteError("not_pushed", "Push the branch before opening a pull request")

        remote = self._remote(row)
        pr_title = f"{patch.task_key}: {title}"[:200]
        body = self._pr_body(patch, title, self._task_text(patch))
        token = self._api_token(row)
        if token is not None and api_supported(row.provider) and remote.scheme != "local":
            try:
                created = self._provider.create_pull_request(
                    PullRequestInput(row.provider, remote, head, row.base_branch, pr_title, body),
                    token,
                )
            except ProviderApiError as exc:
                raise GitRemoteError("provider_failed", str(exc)) from None
            return PullRequestOutcome(
                "created", created.url, head, row.base_branch, pr_title, "Pull request created"
            )
        link = compare_url(row.provider, remote, row.base_branch, head)
        note = (
            "Open the link to create the pull request"
            if link
            else "This provider has no compare link; open a pull request from the branch by hand"
        )
        return PullRequestOutcome("compare_link", link, head, row.base_branch, pr_title, note)

    def _task_text(self, patch: CodePatch) -> str:
        task = self._session.get(PlanTask, patch.task_id)
        if task is None:
            return ""
        lines = [task.description.strip()[:1500]]
        if task.acceptance_criteria:
            lines += ["", "Acceptance criteria:", *[f"- {c}" for c in task.acceptance_criteria]]
        return "\n".join(lines)

    @staticmethod
    def _pr_body(patch: CodePatch, title: str, task_text: str) -> str:
        parts = [f"## {patch.task_key}: {title}", "", task_text, ""]
        for heading, items in (
            ("Patch notes", patch.notes),
            ("Assumptions", patch.assumptions),
            ("Suggested tests", patch.test_suggestions),
        ):
            if items:
                parts += [f"{heading}:", *[f"- {i}" for i in items], ""]
        parts.append(
            f"Patch v{patch.version}, accepted by {patch.reviewed_by or 'unknown'}. "
            "Opened by ArchPilotAI for human review; it is never merged automatically."
        )
        return "\n".join(parts)[:60_000]
