import json
import uuid
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

PROMPT_VERSION = "architecture-v1"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class DecisionOrigin(StrEnum):
    REQUIRED_BY_CLIENT = "required_by_client"
    RECOMMENDED_BY_AI = "recommended_by_ai"
    ASSUMED = "assumed"
    REQUIRES_CLARIFICATION = "requires_clarification"


class NodeType(StrEnum):
    CLIENT = "client"
    API = "api"
    SERVICE = "service"
    DATABASE = "database"
    STORAGE = "storage"
    QUEUE = "queue"
    CACHE = "cache"
    IDENTITY = "identity"
    EXTERNAL = "external"
    OTHER = "other"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ArchitectureError(Exception):
    """Generation failed. `code` is stable and safe; messages never echo model output."""

    code = "architecture_failed"


class InvalidArchitectureOutputError(ArchitectureError):
    code = "invalid_output"


class FabricatedReferenceError(ArchitectureError):
    code = "fabricated_reference"


class NoConfirmedRequirementsError(ArchitectureError):
    code = "no_confirmed_requirements"


class ContextTooLargeError(ArchitectureError):
    code = "context_too_large"


Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
Ref = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
ReqIds = Annotated[list[uuid.UUID], Field(max_length=50)]
TextList = Annotated[list[Text], Field(max_length=10)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Decision(_Strict):
    """A major architecture decision. `origin` says who stands behind it."""

    name: Short
    decision: Text
    rationale: Text
    origin: DecisionOrigin
    supporting_requirement_ids: ReqIds = Field(default_factory=list)
    alternatives_considered: TextList = Field(default_factory=list)
    assumptions: TextList = Field(default_factory=list)
    risks: TextList = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_matches_origin(self) -> "Decision":
        # A client requirement or an AI recommendation must point at confirmed evidence;
        # only assumptions and open points may stand without it.
        needs_evidence = self.origin in (
            DecisionOrigin.REQUIRED_BY_CLIENT,
            DecisionOrigin.RECOMMENDED_BY_AI,
        )
        if needs_evidence and not self.supporting_requirement_ids:
            raise ValueError(f"{self.origin.value} decision needs supporting requirements")
        return self


class StackItem(Decision):
    layer: Short
    technology: Short


class Component(_Strict):
    id: Ref
    name: Short
    type: NodeType
    responsibility: Text
    technology: Short | None = None
    requirement_ids: ReqIds = Field(default_factory=list)


class Risk(_Strict):
    description: Text
    severity: Severity
    mitigation: Text
    requirement_ids: ReqIds = Field(default_factory=list)


class Assumption(_Strict):
    statement: Text
    requirement_ids: ReqIds = Field(default_factory=list)


class OpenQuestion(_Strict):
    question: Text
    why_it_matters: Text | None = None
    requirement_ids: ReqIds = Field(default_factory=list)


class Alternative(_Strict):
    title: Short
    description: Text
    trade_offs: Text
    requirement_ids: ReqIds = Field(default_factory=list)


class Phase(_Strict):
    name: Short
    objectives: Annotated[list[Text], Field(min_length=1, max_length=10)]
    requirement_ids: ReqIds = Field(default_factory=list)


class DiagramNode(_Strict):
    id: Ref
    name: Short
    type: NodeType


class DiagramEdge(_Strict):
    source: Ref
    target: Ref
    relationship: Short


class Diagram(_Strict):
    """Pure data. It is only ever drawn by the client, never executed."""

    nodes: Annotated[list[DiagramNode], Field(min_length=1, max_length=40)]
    edges: Annotated[list[DiagramEdge], Field(max_length=80)] = Field(default_factory=list)

    @model_validator(mode="after")
    def _graph_is_consistent(self) -> "Diagram":
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("diagram node ids must be unique")
        known = set(ids)
        seen: set[tuple[str, str, str]] = set()
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise ValueError("diagram edge references an unknown node")
            if edge.source == edge.target:
                raise ValueError("diagram edge must connect two different nodes")
            key = (edge.source, edge.target, edge.relationship)
            if key in seen:
                raise ValueError("diagram contains a duplicate edge")
            seen.add(key)
        return self


class ArchitectureContent(_Strict):
    solution_overview: Long
    recommended_stack: Annotated[list[StackItem], Field(max_length=20)]
    components: Annotated[list[Component], Field(min_length=1, max_length=30)]
    integrations: Annotated[list[Decision], Field(max_length=20)]
    data_architecture: Annotated[list[Decision], Field(max_length=15)]
    security_architecture: Annotated[list[Decision], Field(max_length=15)]
    non_functional_design: Annotated[list[Decision], Field(max_length=15)]
    risks: Annotated[list[Risk], Field(max_length=20)]
    assumptions: Annotated[list[Assumption], Field(max_length=20)]
    open_questions: Annotated[list[OpenQuestion], Field(max_length=20)]
    alternatives: Annotated[list[Alternative], Field(max_length=15)]
    implementation_phases: Annotated[list[Phase], Field(min_length=1, max_length=12)]
    diagram: Diagram

    @model_validator(mode="after")
    def _component_ids_unique(self) -> "ArchitectureContent":
        ids = [c.id for c in self.components]
        if len(set(ids)) != len(ids):
            raise ValueError("component ids must be unique")
        return self


class GeneratedArchitecture(ArchitectureContent):
    title: Short
    summary: Long


def _id_lists(content: ArchitectureContent) -> Iterable[list[uuid.UUID]]:
    for group in (
        content.recommended_stack,
        content.integrations,
        content.data_architecture,
        content.security_architecture,
        content.non_functional_design,
    ):
        for decision in group:
            yield decision.supporting_requirement_ids
    for section in (
        content.components,
        content.risks,
        content.assumptions,
        content.open_questions,
        content.alternatives,
        content.implementation_phases,
    ):
        for entry in section:
            yield entry.requirement_ids


def referenced_requirement_ids(content: ArchitectureContent) -> set[uuid.UUID]:
    return {rid for ids in _id_lists(content) for rid in ids}


def verify_references(content: ArchitectureContent, confirmed: set[uuid.UUID]) -> None:
    """Every cited requirement must exist in this project and be confirmed."""
    if not referenced_requirement_ids(content) <= confirmed:
        raise FabricatedReferenceError(
            "The proposal cites requirements that are not confirmed requirements of this project"
        )


def parse_generated(raw: str) -> GeneratedArchitecture:
    try:
        return GeneratedArchitecture.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        # Neither the model text nor the validation detail is surfaced.
        raise InvalidArchitectureOutputError(
            "The model output was not a valid architecture proposal"
        ) from exc


def content_to_json(content: ArchitectureContent) -> dict[str, Any]:
    data: dict[str, Any] = content.model_dump(mode="json", exclude={"title", "summary"})
    return data
