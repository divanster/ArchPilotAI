import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.api.ai import _upstream_error, require_non_production
from app.api.deps import PatchReviewServiceDep, PatchServiceDep
from app.core.config import get_settings
from app.domain.patch import PatchInvalidError, PatchOutputError
from app.llm.exceptions import LLMError
from app.models.patch import CodePatch
from app.services.patch import (
    PatchBusyError,
    PatchContextTooLargeError,
    PatchDetail,
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
from app.services.workspace_export import (
    ExportError,
    ForeignFilesError,
    WorkspaceEmptyError,
    WorkspaceLocationError,
    WorkspaceNotConfiguredError,
    export_workspace,
)

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["patches"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

NotFound = (ProjectNotFoundError, PlanNotFoundError, TaskNotFoundError, PatchNotFoundError)


class TechnologyConfirmationIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class TechnologyConfirmationOut(BaseModel):
    task_key: str
    name: str
    reason: str
    created_at: datetime


class GenerateIn(BaseModel):
    acknowledge_blocking_questions: bool = False


class RemovalConfirmationIn(BaseModel):
    file: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)


class ReviewIn(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    confirmed_removals: list[RemovalConfirmationIn] = Field(default_factory=list, max_length=100)


class RemovalItemOut(BaseModel):
    kind: str
    name: str
    file: str
    listed: bool
    reason: str


class RemovalMissingOut(BaseModel):
    file: str
    name: str


class RemovalsNotConfirmedBody(BaseModel):
    code: Literal["removals_not_confirmed"] = "removals_not_confirmed"
    message: str
    missing: list[RemovalMissingOut]


class ExportIn(BaseModel):
    confirm_overwrite: bool = False


class FailureOut(BaseModel):
    code: str
    path: str | None = None


class PatchFileOut(BaseModel):
    path: str
    change: str
    content: str
    previous_content_hash: str | None
    diff: str
    diff_truncated: bool


class PatchSummary(BaseModel):
    id: uuid.UUID
    task_key: str
    version: int
    status: str
    provider: str
    model: str
    prompt_version: str
    created_at: datetime
    reviewed_by: str | None
    review_reason: str | None
    file_count: int = 0
    based_on_version: int | None = None


class PatchResponse(PatchSummary):
    plan_id: uuid.UUID
    task_id: uuid.UUID
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    notes: list[str]
    assumptions: list[str]
    test_suggestions: list[str]
    warnings: list[str]
    blocking_acknowledged: bool
    coverage: dict[str, Any] | None = None
    removed_from_accepted: list[RemovalItemOut] = Field(default_factory=list)
    needs_justification: bool = False
    # Generation only stores patches that passed validation; "stale" is checked against the
    # current accepted workspace while the patch is still proposed.
    validation: str
    stale: list[FailureOut]
    files: list[PatchFileOut]


class WorkspaceFileOut(BaseModel):
    path: str
    content_hash: str
    size: int
    source_patch_id: uuid.UUID


class WorkspaceResponse(BaseModel):
    files: list[WorkspaceFileOut]


class ExportResponse(BaseModel):
    folder: str
    written: list[str]
    unchanged: list[str]
    overwritten: list[str]
    left_on_disk: list[str]
    foreign_untouched: int


def _summary(patch: CodePatch, file_count: int = 0) -> PatchSummary:
    return PatchSummary(
        id=patch.id,
        task_key=patch.task_key,
        version=patch.version,
        status=patch.status,
        provider=patch.provider,
        model=patch.model,
        prompt_version=patch.prompt_version,
        created_at=patch.created_at,
        reviewed_by=patch.reviewed_by,
        review_reason=patch.review_reason,
        file_count=file_count,
        based_on_version=patch.based_on_version,
    )


def _full(detail: PatchDetail) -> PatchResponse:
    p = detail.patch
    return PatchResponse(
        **_summary(p, len(detail.files)).model_dump(),
        plan_id=p.plan_id,
        task_id=p.task_id,
        input_tokens=p.input_tokens,
        output_tokens=p.output_tokens,
        latency_ms=p.latency_ms,
        notes=list(p.notes),
        assumptions=list(p.assumptions),
        test_suggestions=list(p.test_suggestions),
        warnings=list(p.warnings),
        blocking_acknowledged=p.blocking_acknowledged,
        coverage=p.coverage,
        removed_from_accepted=[
            RemovalItemOut.model_validate(i)
            for i in (p.coverage or {}).get("removed_from_accepted", [])
        ],
        needs_justification=bool((p.coverage or {}).get("needs_justification"))
        and p.status == "proposed",
        validation="stale" if detail.stale else "passed",
        stale=[FailureOut(code=f.code, path=f.path) for f in detail.stale],
        files=[
            PatchFileOut(
                path=v.file.path,
                change=v.file.change,
                content=v.file.content,
                previous_content_hash=v.file.previous_content_hash,
                diff=v.diff,
                diff_truncated=v.diff_truncated,
            )
            for v in detail.views
        ],
    )


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.post(
    "/plans/{plan_id}/tasks/{task_key}/technology-confirmation",
    response_model=TechnologyConfirmationOut,
    status_code=status.HTTP_201_CREATED,
)
def confirm_technology(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    body: TechnologyConfirmationIn,
    service: PatchReviewServiceDep,
) -> TechnologyConfirmationOut:
    try:
        saved = service.confirm_technology(project_id, plan_id, task_key, body.name, body.reason)
    except NotFound as exc:
        raise _not_found(exc) from exc
    except PatchGateError as exc:
        raise _conflict(exc) from exc
    except ReviewInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return TechnologyConfirmationOut(
        task_key=saved.task_key, name=saved.name, reason=saved.reason, created_at=saved.created_at
    )


@router.get(
    "/plans/{plan_id}/tasks/{task_key}/technology-confirmation",
    response_model=list[TechnologyConfirmationOut],
)
def list_confirmations(
    project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, service: PatchReviewServiceDep
) -> list[TechnologyConfirmationOut]:
    try:
        rows = service.confirmations(project_id, plan_id, task_key)
    except NotFound as exc:
        raise _not_found(exc) from exc
    return [
        TechnologyConfirmationOut(
            task_key=r.task_key, name=r.name, reason=r.reason, created_at=r.created_at
        )
        for r in rows
    ]


@router.post(
    "/plans/{plan_id}/tasks/{task_key}/patches",
    response_model=PatchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_patch(
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_key: str,
    service: PatchServiceDep,
    body: GenerateIn | None = None,
) -> PatchResponse:
    acknowledge = body.acknowledge_blocking_questions if body else False
    try:
        detail = await run_in_threadpool(
            lambda: service.generate(
                project_id, plan_id, task_key, acknowledge_blocking_questions=acknowledge
            )
        )
    except NotFound as exc:
        raise _not_found(exc) from exc
    except PatchGateError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "message": str(exc)}
        ) from exc
    except PatchBusyError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except PatchContextTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    except PatchInvalidError as exc:
        # Fixed codes and paths only; the rejected content is never echoed back.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": str(exc), "failures": [f.as_json() for f in exc.failures[:50]]},
        ) from exc
    except PatchOutputError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"code": exc.code, "message": str(exc)}
        ) from exc
    except LLMError as exc:
        raise _upstream_error(exc) from exc
    return _full(detail)


@router.get("/plans/{plan_id}/tasks/{task_key}/patches", response_model=list[PatchSummary])
def list_patches(
    project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, service: PatchReviewServiceDep
) -> list[PatchSummary]:
    try:
        found = list(service.for_task(project_id, plan_id, task_key))
        counts = service.file_counts(project_id, [p.id for p in found])
        return [_summary(p, counts.get(p.id, 0)) for p in found]
    except NotFound as exc:
        raise _not_found(exc) from exc


@router.get("/patches/{patch_id}", response_model=PatchResponse)
def get_patch(
    project_id: uuid.UUID, patch_id: uuid.UUID, service: PatchReviewServiceDep
) -> PatchResponse:
    try:
        return _full(service.detail(project_id, patch_id))
    except NotFound as exc:
        raise _not_found(exc) from exc


def _review(
    project_id: uuid.UUID,
    patch_id: uuid.UUID,
    body: ReviewIn,
    service: PatchReviewServiceDep,
    accept: bool,
) -> PatchResponse:
    method = service.accept if accept else service.reject
    try:
        if accept:
            confirmed = [(c.file, c.name, c.reason) for c in body.confirmed_removals]
            result = service.accept(project_id, patch_id, body.reviewer, body.reason, confirmed)
        else:
            result = method(project_id, patch_id, body.reviewer, body.reason)
        return _full(result)
    except NotFound as exc:
        raise _not_found(exc) from exc
    except (PatchNotProposedError, StalePatchError) as exc:
        raise _conflict(exc) from exc
    except RemovalsNotConfirmedError as exc:
        conflict = RemovalsNotConfirmedBody(
            message=str(exc),
            missing=[RemovalMissingOut(file=f, name=n) for f, n in exc.missing[:100]],
        )
        raise HTTPException(status.HTTP_409_CONFLICT, conflict.model_dump()) from exc
    except ReviewInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/patches/{patch_id}/accept", response_model=PatchResponse)
def accept_patch(
    project_id: uuid.UUID, patch_id: uuid.UUID, body: ReviewIn, service: PatchReviewServiceDep
) -> PatchResponse:
    return _review(project_id, patch_id, body, service, True)


@router.post("/patches/{patch_id}/reject", response_model=PatchResponse)
def reject_patch(
    project_id: uuid.UUID, patch_id: uuid.UUID, body: ReviewIn, service: PatchReviewServiceDep
) -> PatchResponse:
    return _review(project_id, patch_id, body, service, False)


@router.get("/workspace", response_model=WorkspaceResponse)
def get_workspace(project_id: uuid.UUID, service: PatchReviewServiceDep) -> WorkspaceResponse:
    try:
        files = service.workspace(project_id)
    except NotFound as exc:
        raise _not_found(exc) from exc
    return WorkspaceResponse(
        files=[
            WorkspaceFileOut(
                path=f.path,
                content_hash=f.content_hash,
                size=len(f.content.encode("utf-8")),
                source_patch_id=f.source_patch_id,
            )
            for f in files
        ]
    )


@router.post("/workspace/export", response_model=ExportResponse)
def export(
    project_id: uuid.UUID, service: PatchReviewServiceDep, body: ExportIn | None = None
) -> ExportResponse:
    try:
        files = service.workspace(project_id)
        result = export_workspace(
            get_settings().workspace_root,
            project_id,
            files,
            confirm_overwrite=body.confirm_overwrite if body else False,
        )
    except NotFound as exc:
        raise _not_found(exc) from exc
    except ForeignFilesError as exc:
        detail: dict[str, Any] = {
            "code": "foreign_files",
            "message": str(exc),
            "paths": exc.paths,
            "total": exc.total,
        }
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    except (WorkspaceNotConfiguredError, WorkspaceEmptyError) as exc:
        raise _conflict(exc) from exc
    except (WorkspaceLocationError, ExportError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return ExportResponse(
        folder=result.folder,
        written=result.written,
        unchanged=result.unchanged,
        overwritten=result.overwritten,
        left_on_disk=result.left_on_disk,
        foreign_untouched=result.foreign_untouched,
    )
