import uuid

from fastapi import APIRouter, HTTPException, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from app.api.deps import DocumentServiceDep, MaxUploadBytesDep
from app.api.schemas import DocumentDetail, DocumentSummary
from app.domain.document import (
    DocumentError,
    DocumentTooLargeError,
    InvalidDocumentError,
    UnsupportedDocumentTypeError,
)
from app.services.document import DocumentNotFoundError, DuplicateDocumentError
from app.services.project import ProjectNotFoundError

router = APIRouter(prefix="/api/projects/{project_id}/documents", tags=["documents"])

_ERROR_STATUS: dict[type[Exception], int] = {
    ProjectNotFoundError: status.HTTP_404_NOT_FOUND,
    DocumentNotFoundError: status.HTTP_404_NOT_FOUND,
    DuplicateDocumentError: status.HTTP_409_CONFLICT,
    UnsupportedDocumentTypeError: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    DocumentTooLargeError: status.HTTP_413_CONTENT_TOO_LARGE,
    InvalidDocumentError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


def _http_error(exc: Exception) -> HTTPException:
    code = _ERROR_STATUS.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
    return HTTPException(status_code=code, detail=str(exc))


@router.post("", response_model=DocumentSummary, status_code=status.HTTP_201_CREATED)
async def upload_document(
    project_id: uuid.UUID,
    file: UploadFile,
    service: DocumentServiceDep,
    max_bytes: MaxUploadBytesDep,
) -> DocumentSummary:
    # Read one byte past the limit so oversized files are detected without reading everything.
    data = await file.read(max_bytes + 1)
    try:
        document = await run_in_threadpool(
            service.upload, project_id, file.filename, file.content_type, data
        )
    except (ProjectNotFoundError, DocumentError, DuplicateDocumentError) as exc:
        raise _http_error(exc) from exc
    return DocumentSummary.model_validate(document)


@router.get("", response_model=list[DocumentSummary])
def list_documents(project_id: uuid.UUID, service: DocumentServiceDep) -> list[DocumentSummary]:
    try:
        return [DocumentSummary.model_validate(d) for d in service.list(project_id)]
    except ProjectNotFoundError as exc:
        raise _http_error(exc) from exc


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(
    project_id: uuid.UUID, document_id: uuid.UUID, service: DocumentServiceDep
) -> DocumentDetail:
    try:
        return DocumentDetail.model_validate(service.get(project_id, document_id))
    except (ProjectNotFoundError, DocumentNotFoundError) as exc:
        raise _http_error(exc) from exc


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    project_id: uuid.UUID, document_id: uuid.UUID, service: DocumentServiceDep
) -> Response:
    try:
        service.delete(project_id, document_id)
    except (ProjectNotFoundError, DocumentNotFoundError) as exc:
        raise _http_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
