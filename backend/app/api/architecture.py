import uuid
from collections.abc import Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool

from app.api.ai import _upstream_error, require_non_production
from app.api.architecture_schemas import ProposalResponse, ProposalSummary, ProposalUpdate
from app.api.deps import ArchitectureGenerationServiceDep, ArchitectureServiceDep
from app.domain.architecture import (
    ArchitectureError,
    ContextTooLargeError,
    NoConfirmedRequirementsError,
    ProposalStatus,
)
from app.llm.exceptions import LLMError
from app.models.architecture import ArchitectureProposal
from app.services.architecture import (
    ProposalChange,
    ProposalNotEditableError,
    ProposalNotFoundError,
    StaleProposalError,
)
from app.services.architecture_generation import ArchitectureBusyError
from app.services.project import ProjectNotFoundError

router = APIRouter(
    prefix="/api/projects/{project_id}/architecture",
    tags=["architecture"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

NotFound = (ProjectNotFoundError, ProposalNotFoundError)


def _summary(proposal: ArchitectureProposal, stale: Sequence[uuid.UUID]) -> ProposalSummary:
    out = ProposalSummary.model_validate(proposal)
    out.stale_requirement_ids = list(stale)
    return out


def _full(proposal: ArchitectureProposal, stale: Sequence[uuid.UUID]) -> ProposalResponse:
    out = ProposalResponse.model_validate(proposal)
    out.stale_requirement_ids = list(stale)
    return out


@router.post("/generate", response_model=ProposalResponse, status_code=status.HTTP_201_CREATED)
async def generate_architecture(
    project_id: uuid.UUID,
    generator: ArchitectureGenerationServiceDep,
    service: ArchitectureServiceDep,
) -> ProposalResponse:
    try:
        proposal = await run_in_threadpool(generator.generate, project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ArchitectureBusyError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
    except NoConfirmedRequirementsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ContextTooLargeError as exc:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc)) from exc
    except ArchitectureError as exc:
        # Messages are fixed strings; model output is never echoed back.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except LLMError as exc:
        raise _upstream_error(exc) from exc
    return _full(proposal, service.stale_references(project_id, [proposal])[proposal.id])


@router.get("", response_model=list[ProposalSummary])
def list_proposals(
    project_id: uuid.UUID,
    service: ArchitectureServiceDep,
    status_filter: Annotated[
        Literal["draft", "approved", "rejected"] | None, Query(alias="status")
    ] = None,
) -> list[ProposalSummary]:
    try:
        found = service.list(project_id, ProposalStatus(status_filter) if status_filter else None)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    stale = service.stale_references(project_id, found)
    return [_summary(p, stale[p.id]) for p in found]


@router.get("/{proposal_id}", response_model=ProposalResponse)
def get_proposal(
    project_id: uuid.UUID, proposal_id: uuid.UUID, service: ArchitectureServiceDep
) -> ProposalResponse:
    try:
        proposal = service.get(project_id, proposal_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _full(proposal, service.stale_references(project_id, [proposal])[proposal.id])


@router.patch("/{proposal_id}", response_model=ProposalResponse)
def update_proposal(
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: ProposalUpdate,
    service: ArchitectureServiceDep,
) -> ProposalResponse:
    change = ProposalChange(
        title=body.title,
        summary=body.summary,
        content=body.content,
        status=ProposalStatus(body.status) if body.status else None,
    )
    try:
        proposal = service.update(project_id, proposal_id, change)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (ProposalNotEditableError, StaleProposalError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ArchitectureError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return _full(proposal, service.stale_references(project_id, [proposal])[proposal.id])
