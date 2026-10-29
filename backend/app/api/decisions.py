import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.ai import require_non_production
from app.api.deps import DecisionServiceDep
from app.domain.decisions import AREAS
from app.models.decision import ProjectDecision
from app.services.decision import DecisionConflictError, DecisionInputError
from app.services.project import ProjectNotFoundError

router = APIRouter(
    prefix="/api/projects/{project_id}/decisions",
    tags=["decisions"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)


class DecisionIn(BaseModel):
    area: str
    choice: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    name: str = Field(min_length=1, max_length=100)


class DecisionOut(BaseModel):
    id: uuid.UUID
    area: str
    choice: str
    reason: str
    decided_by: str
    decided_at: datetime
    superseded_by: uuid.UUID | None


class SuggestionOut(BaseModel):
    choice: str
    layer: str
    label: str = "AI recommendation"


class DecisionsOut(BaseModel):
    areas: list[str]
    current: list[DecisionOut]
    history: list[DecisionOut]
    suggestions: dict[str, SuggestionOut]


def _out(row: ProjectDecision) -> DecisionOut:
    return DecisionOut(
        id=row.id,
        area=row.area,
        choice=row.choice,
        reason=row.reason,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        superseded_by=row.superseded_by,
    )


def _not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")


@router.get("", response_model=DecisionsOut)
def list_decisions(project_id: uuid.UUID, service: DecisionServiceDep) -> DecisionsOut:
    try:
        view = service.view(project_id)
    except ProjectNotFoundError:
        raise _not_found() from None
    return DecisionsOut(
        areas=list(AREAS),
        current=[_out(r) for r in view.current],
        history=[_out(r) for r in view.history],
        suggestions={a: SuggestionOut(**s) for a, s in view.suggestions.items()},
    )


@router.post("", response_model=DecisionOut, status_code=status.HTTP_201_CREATED)
def decide(project_id: uuid.UUID, body: DecisionIn, service: DecisionServiceDep) -> DecisionOut:
    try:
        row = service.decide(project_id, body.area, body.choice, body.reason, body.name)
    except ProjectNotFoundError:
        raise _not_found() from None
    except DecisionInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    except DecisionConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return _out(row)
