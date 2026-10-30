"""Provider API calls for a personal access token: a read-only check and pull-request creation.

Requests go through an injected httpx client (tests use a mock transport). The token is sent only
in a header; error messages carry the HTTP status, never the response body or the token. There is
no merge call anywhere: a pull request is only ever opened.
"""

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.domain.git_remote import RemoteRef, azure_parts

TIMEOUT_SECONDS = 15.0


class ProviderApiError(Exception):
    """The provider API refused or failed; the message is safe to show."""


@dataclass(frozen=True)
class PullRequestInput:
    provider: str
    remote: RemoteRef
    head: str
    base: str
    title: str
    body: str


@dataclass(frozen=True)
class PullRequestResult:
    url: str
    number: int | None


def api_supported(provider: str) -> bool:
    return provider in {"github", "github_enterprise", "gitlab", "azure_devops"}


def _github_base(provider: str, remote: RemoteRef) -> str:
    if provider == "github":
        return "https://api.github.com"
    return f"https://{remote.host}/api/v3"


def _headers(provider: str, token: str) -> dict[str, str]:
    if provider == "gitlab":
        return {"PRIVATE-TOKEN": token, "Accept": "application/json"}
    if provider == "azure_devops":
        encoded = base64.b64encode(f":{token}".encode()).decode()
        return {"Authorization": f"Basic {encoded}", "Accept": "application/json"}
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def _repo_url(provider: str, remote: RemoteRef) -> str:
    seg = remote.segments
    if provider in {"github", "github_enterprise"}:
        return f"{_github_base(provider, remote)}/repos/{quote(seg[0])}/{quote(seg[1])}"
    if provider == "gitlab":
        return f"https://{remote.host}/api/v4/projects/{quote(remote.path, safe='')}"
    org, project, repo = azure_parts(remote)
    return (
        f"https://dev.azure.com/{quote(org)}/{quote(project)}/_apis/git/repositories/{quote(repo)}"
    )


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _fail(action: str, status: int) -> ProviderApiError:
    hint = " (check the token and its scopes)" if status in {401, 403, 404} else ""
    return ProviderApiError(f"The provider refused {action}: HTTP {status}{hint}")


class ProviderClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)

    def check_read(self, provider: str, remote: RemoteRef, token: str) -> None:
        """Read-only call: fetch the repository."""
        url = _repo_url(provider, remote)
        params = {"api-version": "7.1"} if provider == "azure_devops" else None
        try:
            response = self._client.get(url, headers=_headers(provider, token), params=params)
        except httpx.HTTPError as exc:
            raise ProviderApiError(
                f"The provider API is unreachable ({type(exc).__name__})"
            ) from None
        if response.status_code != 200:
            raise _fail("the read-only check", response.status_code)

    def create_pull_request(self, data: PullRequestInput, token: str) -> PullRequestResult:
        provider = data.provider
        base_url = _repo_url(provider, data.remote)
        headers = _headers(provider, token)
        params: dict[str, str] | None = None
        if provider in {"github", "github_enterprise"}:
            url = f"{base_url}/pulls"
            payload: dict[str, Any] = {
                "title": data.title,
                "body": data.body,
                "head": data.head,
                "base": data.base,
            }
        elif provider == "gitlab":
            url = f"{base_url}/merge_requests"
            payload = {
                "title": data.title,
                "description": data.body,
                "source_branch": data.head,
                "target_branch": data.base,
            }
        else:
            url = f"{base_url}/pullrequests"
            params = {"api-version": "7.1"}
            payload = {
                "title": data.title,
                "description": data.body,
                "sourceRefName": f"refs/heads/{data.head}",
                "targetRefName": f"refs/heads/{data.base}",
            }
        try:
            response = self._client.post(url, headers=headers, params=params, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderApiError(
                f"The provider API is unreachable ({type(exc).__name__})"
            ) from None
        if response.status_code not in {200, 201}:
            raise _fail("the pull request", response.status_code)
        return _parse_created(provider, data.remote, _json(response))


def _parse_created(provider: str, remote: RemoteRef, body: dict[str, Any]) -> PullRequestResult:
    if provider in {"github", "github_enterprise"}:
        url, number = body.get("html_url"), body.get("number")
    elif provider == "gitlab":
        url, number = body.get("web_url"), body.get("iid")
    else:
        number = body.get("pullRequestId")
        org, project, repo = azure_parts(remote)
        url = (
            f"https://dev.azure.com/{quote(org)}/{quote(project)}/_git/{quote(repo)}"
            f"/pullrequest/{number}"
            if number is not None
            else None
        )
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ProviderApiError("The provider answered without a pull request link")
    return PullRequestResult(url, number if isinstance(number, int) else None)
