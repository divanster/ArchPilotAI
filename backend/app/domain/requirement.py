import json
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

MAX_REQUIREMENTS_PER_RUN = 25
MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 5000
MAX_QUOTE_CHARS = 2000
PROMPT_VERSION = "requirements-v1"


class RequirementType(StrEnum):
    BUSINESS = "business"
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    CONSTRAINT = "constraint"


class RequirementStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ExtractionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExtractionError(Exception):
    """Extraction failed. `code` is a stable, safe identifier; messages never echo model output."""

    code = "extraction_failed"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidModelOutputError(ExtractionError):
    code = "invalid_output"


class UnsupportedRequirementError(ExtractionError):
    code = "unsupported_requirement"


class DocumentTooLargeForExtractionError(ExtractionError):
    code = "document_too_large"


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_DESCRIPTION_CHARS)
]


class RequirementDraft(BaseModel):
    """One model-proposed requirement. Unknown fields (e.g. offsets) are rejected, never used."""

    model_config = ConfigDict(extra="forbid")

    type: RequirementType
    title: Title
    description: Description
    # Verbatim, not stripped: it must match the document exactly.
    source_quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)
    # 1-based index of the occurrence when the quote appears more than once.
    occurrence: int | None = Field(default=None, ge=1)


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[RequirementDraft] = Field(max_length=MAX_REQUIREMENTS_PER_RUN)


class LocatedRequirement(BaseModel):
    draft: RequirementDraft
    source_start: int
    source_end: int


def parse_output(raw: str) -> ExtractionOutput:
    try:
        data = json.loads(raw)
        return ExtractionOutput.model_validate(data)
    except (ValueError, ValidationError) as exc:
        # Neither the model text nor the validation detail is surfaced.
        raise InvalidModelOutputError(
            "The model output was not valid structured requirements"
        ) from exc


def _occurrences(text: str, quote: str) -> list[int]:
    found: list[int] = []
    start = text.find(quote)
    while start != -1:
        found.append(start)
        start = text.find(quote, start + 1)
    return found


def locate(text: str, draft: RequirementDraft) -> LocatedRequirement:
    """Offsets are computed here from the document; the model never supplies them."""
    if not draft.source_quote.strip():
        raise UnsupportedRequirementError("A requirement cites a blank source quote")
    positions = _occurrences(text, draft.source_quote)
    if not positions:
        raise UnsupportedRequirementError(
            "A requirement cites a quote that does not occur in the document"
        )
    if draft.occurrence is None:
        if len(positions) > 1:
            raise UnsupportedRequirementError(
                "A requirement cites a quote that occurs more than once without an occurrence"
            )
        start = positions[0]
    else:
        if draft.occurrence > len(positions):
            raise UnsupportedRequirementError("A requirement cites a nonexistent quote occurrence")
        start = positions[draft.occurrence - 1]
    return LocatedRequirement(
        draft=draft, source_start=start, source_end=start + len(draft.source_quote)
    )
