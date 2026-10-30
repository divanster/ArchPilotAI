"""The code map of a project's connected repository (Phase 13C): status and manual refresh."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.ai import require_non_production
from app.api.deps import SessionDep
from app.core.config import get_settings
from app.services.project import ProjectNotFoundError
from app.services.repo_map import RepoMapService, RepositoryUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/repository",
    tags=["repository"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)


def _service(session: Session) -> RepoMapService:
    return RepoMapService(session, get_settings().workspace_root)


@router.get("")
def read_repository(project_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    try:
        return _service(session).status(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/refresh")
async def refresh_repository(project_id: uuid.UUID, session: SessionDep) -> dict[str, Any]:
    service = _service(session)
    try:
        await run_in_threadpool(service.refresh, project_id)
        return service.status(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except RepositoryUnavailableError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "message": str(exc)}
        ) from exc


def refresh_quietly(session: Session, workspace_root: str, project_id: uuid.UUID) -> None:
    """Rescan after a clone, attach or new local revision. Never fails the calling action."""
    try:
        RepoMapService(session, workspace_root).refresh(project_id)
    except Exception:
        logger.warning("repository map refresh failed project=%s", project_id, exc_info=True)
