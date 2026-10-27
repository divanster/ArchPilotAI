import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.domain.project import ProjectStatus

ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ProjectDescription = Annotated[str, StringConstraints(max_length=10_000)]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProjectName
    description: ProjectDescription | None = None
    status: ProjectStatus = ProjectStatus.DRAFT


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ProjectName | None = None
    description: ProjectDescription | None = None
    status: ProjectStatus | None = None

    @field_validator("name", "status")
    @classmethod
    def _not_null_when_sent(cls, value: object) -> object:
        # Only runs for explicitly sent values; omitted fields use the default.
        if value is None:
            raise ValueError("must not be null")
        return value


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    filename: str = Field(validation_alias="original_filename")
    media_type: str
    size_bytes: int
    content_sha256: str
    created_at: datetime


class DocumentDetail(DocumentSummary):
    text: str = Field(validation_alias="extracted_text")
