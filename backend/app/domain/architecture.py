import json
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
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
from pydantic_core import ErrorDetails, PydanticCustomError

PROMPT_VERSION = "architecture-v3"


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


class FailureCategory(StrEnum):
    """Why model output was refused. Every value is a fixed, content-free label."""

    JSON_PARSE = "json_parse"
    SCHEMA = "schema"
    REFERENCE = "reference"
    GRAPH = "graph"
    OUTPUT_SIZE = "output_size"


# Fixed error types raised by the custom validators below (instead of free-text messages).
EVIDENCE_REQUIRED = "evidence_required"
DUPLICATE_COMPONENT_ID = "duplicate_component_id"
DUPLICATE_NODE_ID = "duplicate_node_id"
UNKNOWN_EDGE_NODE = "unknown_edge_node"
SELF_LOOP_EDGE = "self_loop_edge"
DUPLICATE_EDGE = "duplicate_edge"
GRAPH_CODES = frozenset({DUPLICATE_NODE_ID, UNKNOWN_EDGE_NODE, SELF_LOOP_EDGE, DUPLICATE_EDGE})
SIZE_LIMIT_TYPES = frozenset({"string_too_long", "too_long"})
MAX_LOGGED_ERRORS = 25
RESPONSE_CHARS_PER_TOKEN = 8  # generous ceiling: a reply beyond this ignored the output cap


@dataclass(frozen=True)
class ProviderMetadata:
    """What the provider reported about a response; None where it reported nothing."""

    provider: str | None = None
    model: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None

    def as_dict(self) -> dict[str, str | int | None]:
        return dict(asdict(self))


class ArchitectureError(Exception):
    """Generation failed. `code` is stable and safe; messages never echo model output.

    `category`, `diagnostics` and `metadata` are content-free: fixed labels, counts, schema field
    paths and provider-reported numbers only.
    """

    code = "architecture_failed"

    def __init__(
        self,
        message: str = "",
        *,
        category: FailureCategory | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.diagnostics: dict[str, Any] = diagnostics or {}
        self.metadata: ProviderMetadata | None = None


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
            raise PydanticCustomError(
                EVIDENCE_REQUIRED, "client or AI decisions need supporting requirements"
            )
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
            raise PydanticCustomError(DUPLICATE_NODE_ID, "diagram node ids must be unique")
        known = set(ids)
        seen: set[tuple[str, str, str]] = set()
        for edge in self.edges:
            if edge.source not in known or edge.target not in known:
                raise PydanticCustomError(UNKNOWN_EDGE_NODE, "diagram edge uses an unknown node")
            if edge.source == edge.target:
                raise PydanticCustomError(SELF_LOOP_EDGE, "diagram edge joins a node to itself")
            key = (edge.source, edge.target, edge.relationship)
            if key in seen:
                raise PydanticCustomError(DUPLICATE_EDGE, "diagram contains a duplicate edge")
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
            raise PydanticCustomError(DUPLICATE_COMPONENT_ID, "component ids must be unique")
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
    cited = referenced_requirement_ids(content)
    if not cited <= confirmed:
        raise FabricatedReferenceError(
            "The proposal cites requirements that are not confirmed requirements of this project",
            category=FailureCategory.REFERENCE,
            diagnostics={
                "cited_distinct": len(cited),
                "not_confirmed_distinct": len(cited - confirmed),
                "confirmed_available": len(confirmed),
            },
        )


_SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    Decision,
    StackItem,
    Component,
    Risk,
    Assumption,
    OpenQuestion,
    Alternative,
    Phase,
    DiagramNode,
    DiagramEdge,
    Diagram,
    ArchitectureContent,
    GeneratedArchitecture,
)
_KNOWN_FIELDS = frozenset(name for model in _SCHEMA_MODELS for name in model.model_fields)
_SAFE_TYPE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_SAFE_FINISH = re.compile(r"^[a-z_]{1,32}$")


def safe_finish_reason(value: object) -> str | None:
    """Provider finish reasons are short fixed words; anything else is dropped."""
    return value if isinstance(value, str) and _SAFE_FINISH.match(value) else None


def _safe_path(loc: Sequence[int | str]) -> str:
    """Schema field names and list indexes only; model-chosen names become `?`."""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            name = part if part in _KNOWN_FIELDS else "?"
            path += f".{name}" if path else name
    return path or "<root>"


def _safe_type(error: ErrorDetails) -> str:
    kind = error["type"]
    return kind if _SAFE_TYPE.match(kind) else "unknown"


def summarize_validation_errors(exc: ValidationError) -> dict[str, Any]:
    """Content-free summary: schema paths, error types and counts. No input, ctx or messages."""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    counts = Counter((_safe_path(e["loc"]), _safe_type(e)) for e in errors)
    listed = [
        {"path": path, "type": kind, "count": n}
        for (path, kind), n in list(counts.items())[:MAX_LOGGED_ERRORS]
    ]
    return {
        "error_count": len(errors),
        "errors": listed,
        "errors_omitted": max(0, len(counts) - MAX_LOGGED_ERRORS),
        "size_limit_errors": sum(1 for e in errors if e["type"] in SIZE_LIMIT_TYPES),
    }


def _invalid(
    category: FailureCategory, diagnostics: dict[str, Any]
) -> InvalidArchitectureOutputError:
    return InvalidArchitectureOutputError(
        "The model output was not a valid architecture proposal",
        category=category,
        diagnostics=diagnostics,
    )


def parse_generated(
    raw: str, *, finish_reason: str | None = None, max_chars: int | None = None
) -> GeneratedArchitecture:
    """Strictly parse model output. Failures carry content-free diagnostics; nothing is repaired."""
    base: dict[str, Any] = {"raw_chars": len(raw), "finish_reason": finish_reason}
    if max_chars is not None and len(raw) > max_chars:
        raise _invalid(FailureCategory.OUTPUT_SIZE, {**base, "reason": "response_too_large"})
    try:
        data = json.loads(raw)
    except ValueError as exc:
        truncated = finish_reason == "length"
        diagnostics = {
            **base,
            "reason": "truncated_by_token_limit" if truncated else "invalid_json",
            "error_offset": getattr(exc, "pos", None),
            "starts_with_code_fence": raw.lstrip().startswith("```"),
        }
        category = FailureCategory.OUTPUT_SIZE if truncated else FailureCategory.JSON_PARSE
        raise _invalid(category, diagnostics) from None
    try:
        return GeneratedArchitecture.model_validate(data)
    except ValidationError as exc:
        summary = summarize_validation_errors(exc)
        types = {e["type"] for e in summary["errors"]}
        graph_only = bool(types) and summary["errors_omitted"] == 0 and types <= GRAPH_CODES
        category = FailureCategory.GRAPH if graph_only else FailureCategory.SCHEMA
        raise _invalid(category, {**base, **summary}) from None


def content_to_json(content: ArchitectureContent) -> dict[str, Any]:
    data: dict[str, Any] = content.model_dump(mode="json", exclude={"title", "summary"})
    return data
