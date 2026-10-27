import uuid

from fastapi import APIRouter, HTTPException, Response, status

from app.api.deps import ProjectServiceDep
from app.api.schemas import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.project import ProjectNotFoundError

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _not_found(exc: ProjectNotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(data: ProjectCreate, service: ProjectServiceDep) -> ProjectResponse:
    return ProjectResponse.model_validate(service.create(data))


@router.get("", response_model=list[ProjectResponse])
def list_projects(service: ProjectServiceDep) -> list[ProjectResponse]:
    return [ProjectResponse.model_validate(p) for p in service.list()]


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: uuid.UUID, service: ProjectServiceDep) -> ProjectResponse:
    try:
        return ProjectResponse.model_validate(service.get(project_id))
    except ProjectNotFoundError as exc:
        raise _not_found(exc) from exc


@router.patch("/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: uuid.UUID, data: ProjectUpdate, service: ProjectServiceDep
) -> ProjectResponse:
    try:
        return ProjectResponse.model_validate(service.update(project_id, data))
    except ProjectNotFoundError as exc:
        raise _not_found(exc) from exc


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: uuid.UUID, service: ProjectServiceDep) -> Response:
    try:
        service.delete(project_id)
    except ProjectNotFoundError as exc:
        raise _not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
