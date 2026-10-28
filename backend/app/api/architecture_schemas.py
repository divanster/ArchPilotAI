import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.architecture import ArchitectureContent, ProposalStatus
from app.domain.semantic_lint import LintReport

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Summary = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class ProposalSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    summary: str
    status: ProposalStatus
    version: int
    provider: str
    model: str
    prompt_version: str
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    # Set on a version produced by "revise with findings"; the source version is never changed.
    revised_from_id: uuid.UUID | None = None
    revised_from_version: int | None = None
    revised_from_findings: int | None = None
    # Per finding type {before, resolved, still_present, introduced}; null if not a revision.
    revised_outcome: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    # Cited requirements that are no longer confirmed; such a proposal cannot be approved.
    stale_requirement_ids: list[uuid.UUID] = Field(default_factory=list)


class ProposalResponse(ProposalSummary):
    content: ArchitectureContent
    # Advisory review findings; null for drafts stored before the lint existed (never linted).
    lint_findings: LintReport | None = None


class ProposalUpdate(BaseModel):
    """Review fields only. Provenance (provider, model, version, project) is never editable."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    summary: Summary | None = None
    content: ArchitectureContent | None = None
    status: Literal["approved", "rejected"] | None = None

    @model_validator(mode="after")
    def _valid_change(self) -> "ProposalUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one of title, summary, content, status is required")
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("fields must not be null")
        if self.status is not None and self.model_fields_set != {"status"}:
            # The reviewer approves or rejects exactly what they saw, never an edit in flight.
            raise ValueError("status must be changed on its own, not together with edits")
        return self
