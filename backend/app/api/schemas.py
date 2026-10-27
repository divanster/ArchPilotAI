import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.domain.project import ProjectStatus
from app.domain.requirement import (
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    RequirementStatus,
    RequirementType,
)
from app.llm.contracts import TokenUsage

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


class AIGenerateRequest(BaseModel):
    prompt: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)
    ]


class AIGenerateResponse(BaseModel):
    answer: str
    provider: str
    model: str
    latency_ms: int
    usage: TokenUsage


class RequirementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    document_id: uuid.UUID
    extraction_run_id: uuid.UUID
    type: RequirementType
    title: str
    description: str
    source_quote: str
    source_start: int
    source_end: int
    status: RequirementStatus
    created_at: datetime
    updated_at: datetime


class RequirementUpdate(BaseModel):
    """Only review fields. Project, document and source evidence are not editable."""

    model_config = ConfigDict(extra="forbid")

    title: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_CHARS)
        ]
        | None
    ) = None
    description: (
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True, min_length=1, max_length=MAX_DESCRIPTION_CHARS
            ),
        ]
        | None
    ) = None
    status: Literal["confirmed", "rejected"] | None = None

    @model_validator(mode="after")
    def _something_to_change(self) -> "RequirementUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one of title, description, status is required")
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("fields must not be null")
        return self


class ExtractionRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    document_id: uuid.UUID
    provider: str
    model: str
    prompt_version: str
    status: str
    error_code: str | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    created_at: datetime


class ExtractionResponse(BaseModel):
    run: ExtractionRunResponse
    requirements: list[RequirementResponse]
    skipped_duplicates: int
