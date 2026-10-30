import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.api.ai import require_non_production
from app.api.deps import SessionDep
from app.core.config import get_settings
from app.domain.git_remote import GitSettingsError
from app.models.git_remote import GitRemoteSettings
from app.services.credential_store import CredentialStoreUnavailableError, KeyringCredentialStore
from app.services.git_remote import (
    GitRemoteError,
    GitRemoteService,
    GitSettingsInput,
    SettingsNotFoundError,
)
from app.services.patch import PatchNotFoundError
from app.services.project import ProjectNotFoundError

router = APIRouter(
    prefix="/api/projects/{project_id}/git",
    tags=["git"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

_STATUS = {
    "not_configured": status.HTTP_404_NOT_FOUND,
    "branch_not_found": status.HTTP_404_NOT_FOUND,
    "git_failed": status.HTTP_502_BAD_GATEWAY,
    "provider_failed": status.HTTP_502_BAD_GATEWAY,
}


def get_git_service(session: SessionDep) -> GitRemoteService:
    return GitRemoteService(
        session,
        workspace_root=get_settings().workspace_root,
        store=KeyringCredentialStore(),
    )


GitServiceDep = Annotated[GitRemoteService, Depends(get_git_service)]


class GitSettingsIn(BaseModel):
    provider: str
    remote_url: str = Field(max_length=500)
    clone_folder: str | None = Field(default=None, max_length=200)
    base_branch: str = Field(default="main", max_length=100)
    branch_prefix: str = Field(default="agent/", max_length=50)
    author_name: str = Field(max_length=100)
    author_email: str = Field(max_length=200)
    auth_method: str = "credential_manager"


class PatInfo(BaseModel):
    saved: bool
    provider: str | None
    last4: str | None


class GitSettingsOut(BaseModel):
    provider: str
    remote_url: str
    clone_folder: str
    base_branch: str
    branch_prefix: str
    author_name: str
    author_email: str
    auth_method: str
    pat: PatInfo
    cloned: bool
    updated_at: datetime


class PatIn(BaseModel):
    # Validated by the service, so that a refusal never echoes the value back.
    token: str


class ConnectionOut(BaseModel):
    reachable: bool
    api_checked: bool
    api_ok: bool | None
    message: str


class FolderOut(BaseModel):
    folder: str


class CommitOut(BaseModel):
    branch: str
    commit: str
    created_branch: bool
    files: list[str]


class PushIn(BaseModel):
    branch: str = Field(max_length=200)
    approved_by: str = Field(max_length=100)
    confirm: bool = False


class PushOut(BaseModel):
    branch: str
    pushed: bool


class PullRequestOut(BaseModel):
    mode: str
    url: str | None
    branch: str
    base: str
    title: str
    message: str


def _out(service: GitRemoteService, row: GitRemoteSettings) -> GitSettingsOut:
    return GitSettingsOut(
        provider=row.provider,
        remote_url=row.remote_url,
        clone_folder=row.clone_folder,
        base_branch=row.base_branch,
        branch_prefix=row.branch_prefix,
        author_name=row.author_name,
        author_email=row.author_email,
        auth_method=row.auth_method,
        pat=PatInfo(saved=row.pat_ref is not None, provider=row.pat_provider, last4=row.pat_last4),
        cloned=service.is_cloned(row),
        updated_at=row.updated_at,
    )


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, ProjectNotFoundError | PatchNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, GitSettingsError):
        return HTTPException(422, str(exc))
    if isinstance(exc, CredentialStoreUnavailableError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, GitRemoteError):
        return HTTPException(
            _STATUS.get(exc.code, status.HTTP_409_CONFLICT),
            {"code": exc.code, "message": str(exc)},
        )
    raise exc


_HANDLED = (
    ProjectNotFoundError,
    PatchNotFoundError,
    GitSettingsError,
    CredentialStoreUnavailableError,
    GitRemoteError,
)


@router.get("", response_model=GitSettingsOut)
def read_settings(project_id: uuid.UUID, service: GitServiceDep) -> GitSettingsOut:
    try:
        return _out(service, service.get(project_id))
    except _HANDLED as exc:
        raise _http(exc) from exc


@router.put("", response_model=GitSettingsOut)
def save_settings(
    project_id: uuid.UUID, body: GitSettingsIn, service: GitServiceDep
) -> GitSettingsOut:
    try:
        row = service.save(project_id, GitSettingsInput(**body.model_dump()))
        return _out(service, row)
    except _HANDLED as exc:
        raise _http(exc) from exc


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
def delete_settings(project_id: uuid.UUID, service: GitServiceDep) -> Response:
    try:
        service.delete(project_id)
    except _HANDLED as exc:
        raise _http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/pat", response_model=GitSettingsOut)
def save_pat(project_id: uuid.UUID, body: PatIn, service: GitServiceDep) -> GitSettingsOut:
    try:
        return _out(service, service.save_pat(project_id, body.token))
    except _HANDLED as exc:
        raise _http(exc) from exc


@router.delete("/pat", response_model=GitSettingsOut)
def delete_pat(project_id: uuid.UUID, service: GitServiceDep) -> GitSettingsOut:
    try:
        return _out(service, service.delete_pat(project_id))
    except _HANDLED as exc:
        raise _http(exc) from exc


@router.post("/test", response_model=ConnectionOut)
async def test_connection(project_id: uuid.UUID, service: GitServiceDep) -> ConnectionOut:
    try:
        result = await run_in_threadpool(service.test_connection, project_id)
    except _HANDLED as exc:
        raise _http(exc) from exc
    return ConnectionOut(**result.__dict__)


@router.post("/clone", response_model=FolderOut)
async def clone(project_id: uuid.UUID, service: GitServiceDep) -> FolderOut:
    try:
        return FolderOut(folder=str(await run_in_threadpool(service.clone, project_id)))
    except _HANDLED as exc:
        raise _http(exc) from exc


@router.post("/attach", response_model=FolderOut)
def attach(project_id: uuid.UUID, service: GitServiceDep) -> FolderOut:
    try:
        return FolderOut(folder=str(service.attach(project_id)))
    except _HANDLED as exc:
        raise _http(exc) from exc


@router.post("/patches/{patch_id}/commit", response_model=CommitOut)
def commit_patch(project_id: uuid.UUID, patch_id: uuid.UUID, service: GitServiceDep) -> CommitOut:
    try:
        return CommitOut(**service.commit_patch(project_id, patch_id).__dict__)
    except _HANDLED as exc:
        raise _http(exc) from exc


@router.post("/push", response_model=PushOut)
async def push(project_id: uuid.UUID, body: PushIn, service: GitServiceDep) -> PushOut:
    """Explicit user action. Needs `confirm` and a named approver; never the base branch."""
    if not body.confirm:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "not_confirmed", "message": "Confirm the push"}
        )
    try:
        branch = await run_in_threadpool(service.push, project_id, body.branch, body.approved_by)
    except _HANDLED as exc:
        raise _http(exc) from exc
    return PushOut(branch=branch, pushed=True)


@router.post("/patches/{patch_id}/pull-request", response_model=PullRequestOut)
async def pull_request(
    project_id: uuid.UUID, patch_id: uuid.UUID, service: GitServiceDep
) -> PullRequestOut:
    try:
        result = await run_in_threadpool(service.pull_request, project_id, patch_id)
    except _HANDLED as exc:
        raise _http(exc) from exc
    return PullRequestOut(**result.__dict__)


__all__ = ["SettingsNotFoundError", "router"]
