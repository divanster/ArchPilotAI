import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool

from app.api.ai import _upstream_error, require_non_production
from app.api.deps import ExtractionServiceDep, RequirementServiceDep
from app.api.schemas import (
    ExtractionResponse,
    ExtractionRunResponse,
    RequirementResponse,
    RequirementUpdate,
)
from app.domain.requirement import (
    DocumentTooLargeForExtractionError,
    ExtractionError,
    RequirementStatus,
)
from app.llm.exceptions import LLMError
from app.services.document import DocumentNotFoundError
from app.services.project import ProjectNotFoundError
from app.services.requirement import (
    InvalidTransitionError,
    RequirementChange,
    RequirementNotFoundError,
)
from app.services.requirement_extraction import ExtractionBusyError

router = APIRouter(
    prefix="/api/projects/{project_id}",
    tags=["requirements"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

NotFound = (ProjectNotFoundError, DocumentNotFoundError, RequirementNotFoundError)


@router.post(
    "/documents/{document_id}/extract-requirements",
    response_model=ExtractionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def extract_requirements(
    project_id: uuid.UUID, document_id: uuid.UUID, service: ExtractionServiceDep
) -> ExtractionResponse:
    try:
        result = await run_in_threadpool(service.extract, project_id, document_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ExtractionBusyError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except DocumentTooLargeForExtractionError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    except ExtractionError as exc:
        # Messages are fixed strings; model output is never echoed back.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except LLMError as exc:
        raise _upstream_error(exc) from exc
    return ExtractionResponse(
        run=ExtractionRunResponse.model_validate(result.run),
        requirements=[RequirementResponse.model_validate(r) for r in result.created],
        skipped_duplicates=result.skipped_duplicates,
    )


@router.get("/requirements", response_model=list[RequirementResponse])
def list_requirements(
    project_id: uuid.UUID,
    service: RequirementServiceDep,
    status_filter: Annotated[
        Literal["proposed", "confirmed", "rejected"] | None, Query(alias="status")
    ] = None,
    document_id: uuid.UUID | None = None,
) -> list[RequirementResponse]:
    try:
        found = service.list(
            project_id,
            RequirementStatus(status_filter) if status_filter else None,
            document_id,
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return [RequirementResponse.model_validate(r) for r in found]


@router.get("/requirements/{requirement_id}", response_model=RequirementResponse)
def get_requirement(
    project_id: uuid.UUID, requirement_id: uuid.UUID, service: RequirementServiceDep
) -> RequirementResponse:
    try:
        return RequirementResponse.model_validate(service.get(project_id, requirement_id))
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.patch("/requirements/{requirement_id}", response_model=RequirementResponse)
def update_requirement(
    project_id: uuid.UUID,
    requirement_id: uuid.UUID,
    body: RequirementUpdate,
    service: RequirementServiceDep,
) -> RequirementResponse:
    change = RequirementChange(
        title=body.title,
        description=body.description,
        status=RequirementStatus(body.status) if body.status else None,
    )
    try:
        updated = service.update(project_id, requirement_id, change)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return RequirementResponse.model_validate(updated)
