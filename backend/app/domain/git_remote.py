"""Pure rules for an external Git repository: providers, remote URLs, branch names, links.

Nothing here touches the network, the disk or a credential store.
"""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlsplit

Provider = Literal["github", "github_enterprise", "azure_devops", "gitlab", "generic"]
AuthMethod = Literal["credential_manager", "ssh", "pat"]

PROVIDERS: tuple[str, ...] = (
    "github",
    "github_enterprise",
    "azure_devops",
    "gitlab",
    "generic",
)
AUTH_METHODS: tuple[str, ...] = ("credential_manager", "ssh", "pat")

# The ArchPilotAI repository itself is never a valid target.
OWN_REPO_NAME = "archpilotai"

_SCP_LIKE = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s:][^\s]*)$"
)
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_KEY = re.compile(r"[^A-Za-z0-9._-]+")


class GitSettingsError(ValueError):
    """A settings value is not acceptable (HTTP 422/409). The message never holds a secret."""


@dataclass(frozen=True)
class RemoteRef:
    scheme: str  # https | ssh | local
    host: str
    segments: tuple[str, ...]  # path segments without the trailing ".git"

    @property
    def path(self) -> str:
        return "/".join(self.segments)

    @property
    def normalized(self) -> str:
        """Identity of the repository, whatever the transport: host/path, lower-case."""
        return f"{self.host}/{self.path}".lower()

    @property
    def repo_name(self) -> str:
        return self.segments[-1] if self.segments else ""


def _strip_git(segments: list[str]) -> tuple[str, ...]:
    parts = [s for s in segments if s]
    if parts and parts[-1].lower().endswith(".git"):
        parts[-1] = parts[-1][:-4]
    return tuple(parts)


def parse_remote(url: str, *, allow_local: bool = False) -> RemoteRef:
    """Parse an HTTPS or SSH remote; credentials inside the URL are refused."""
    value = url.strip()
    if not value or value != url or any(c.isspace() or ord(c) < 32 for c in value):
        raise GitSettingsError("The remote URL must not be empty or contain whitespace")
    if value.startswith("-"):
        raise GitSettingsError("The remote URL is not valid")
    lowered = value.lower()
    if lowered.startswith(("https://", "ssh://")):
        parts = urlsplit(value)
        if parts.query or parts.fragment:
            raise GitSettingsError("The remote URL must not hold a query or fragment")
        if parts.scheme == "https" and (parts.username is not None or parts.password is not None):
            raise GitSettingsError(
                "The remote URL must not contain credentials; use the access token setting"
            )
        if parts.scheme == "ssh" and parts.password is not None:
            raise GitSettingsError("The remote URL must not contain a password")
        host = (parts.hostname or "").lower()
        if not _HOST.match(host):
            raise GitSettingsError("The remote URL has no valid host")
        segments = _strip_git(parts.path.split("/"))
        if not segments:
            raise GitSettingsError("The remote URL has no repository path")
        return RemoteRef(parts.scheme, host, segments)
    match = _SCP_LIKE.match(value)
    if match:
        segments = _strip_git(match["path"].split("/"))
        if not segments:
            raise GitSettingsError("The remote URL has no repository path")
        return RemoteRef("ssh", match["host"].lower(), segments)
    if allow_local:
        return RemoteRef("local", "local", _strip_git(value.replace("\\", "/").split("/")))
    raise GitSettingsError("The remote URL must be HTTPS or SSH")


def validate_provider(provider: str, remote: RemoteRef) -> None:
    if provider not in PROVIDERS:
        raise GitSettingsError(f"Unknown provider; use one of {', '.join(PROVIDERS)}")
    if remote.scheme == "local" or provider == "generic":
        return
    host = remote.host
    if provider == "github":
        if host not in {"github.com", "www.github.com"} or len(remote.segments) != 2:
            raise GitSettingsError("A GitHub remote looks like https://github.com/owner/repo")
    elif provider == "github_enterprise":
        if host in {"github.com", "www.github.com"} or len(remote.segments) != 2:
            raise GitSettingsError(
                "A GitHub Enterprise remote looks like https://host/owner/repo (not github.com)"
            )
    elif provider == "gitlab":
        if len(remote.segments) < 2:
            raise GitSettingsError("A GitLab remote looks like https://host/group/project")
    elif provider == "azure_devops":
        azure_https = host == "dev.azure.com" and (
            len(remote.segments) == 4 and remote.segments[2] == "_git"
        )
        azure_ssh = host == "ssh.dev.azure.com" and (
            len(remote.segments) == 4 and remote.segments[0] == "v3"
        )
        if not (azure_https or azure_ssh):
            raise GitSettingsError(
                "An Azure DevOps remote looks like https://dev.azure.com/org/project/_git/repo"
            )


def validate_auth(auth_method: str, remote: RemoteRef) -> None:
    if auth_method not in AUTH_METHODS:
        raise GitSettingsError(f"Unknown auth method; use one of {', '.join(AUTH_METHODS)}")
    if remote.scheme == "local":
        return
    if auth_method == "ssh" and remote.scheme != "ssh":
        raise GitSettingsError("SSH authentication needs an SSH remote URL")
    if auth_method == "pat" and remote.scheme != "https":
        raise GitSettingsError("A personal access token needs an HTTPS remote URL")


def validate_branch_name(name: str, what: str = "branch") -> str:
    value = name.strip()
    if (
        not _BRANCH.match(value)
        or ".." in value
        or "//" in value
        or "@{" in value
        or value.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    ):
        raise GitSettingsError(f"The {what} name is not a valid Git branch name")
    return value


def validate_prefix(prefix: str) -> str:
    value = prefix.strip()
    if not value:
        return ""
    if not _PREFIX.match(value) or ".." in value or "//" in value or value.startswith("."):
        raise GitSettingsError("The branch prefix is not valid")
    return value


def is_own_repository(remote: RemoteRef, own_remotes: list[RemoteRef]) -> bool:
    if remote.repo_name.lower() == OWN_REPO_NAME:
        return True
    return any(remote.normalized == own.normalized for own in own_remotes)


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return slug or "task"


def branch_name(prefix: str, task_key: str, title: str) -> str:
    key = _KEY.sub("-", task_key.strip()).strip("-.") or "task"
    return f"{prefix}{key}-{slugify(title)}"


def compare_url(provider: str, remote: RemoteRef, base: str, head: str) -> str | None:
    """Provider-specific link that opens the "new pull request" page for head -> base."""
    if remote.scheme == "local":
        return None
    seg = remote.segments
    if provider in {"github", "github_enterprise"}:
        host = "github.com" if provider == "github" else remote.host
        return f"https://{host}/{seg[0]}/{seg[1]}/compare/{_ref(base)}...{_ref(head)}?expand=1"
    if provider == "gitlab":
        path = quote(remote.path, safe="/")
        return (
            f"https://{remote.host}/{path}/-/merge_requests/new"
            f"?merge_request%5Bsource_branch%5D={quote(head, safe='')}"
            f"&merge_request%5Btarget_branch%5D={quote(base, safe='')}"
        )
    if provider == "azure_devops":
        org, project, repo = azure_parts(remote)
        return (
            f"https://dev.azure.com/{quote(org)}/{quote(project)}/_git/{quote(repo)}"
            f"/pullrequestcreate?sourceRef={quote(head, safe='')}&targetRef={quote(base, safe='')}"
        )
    return None


def _ref(name: str) -> str:
    return quote(name, safe="/")


def azure_parts(remote: RemoteRef) -> tuple[str, str, str]:
    seg = remote.segments
    if remote.scheme == "ssh":  # v3/org/project/repo
        return seg[1], seg[2], seg[3]
    return seg[0], seg[1], seg[3]  # org/project/_git/repo
