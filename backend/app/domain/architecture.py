import copy
import json
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_core import ErrorDetails, PydanticCustomError

PROMPT_VERSION = "architecture-v6"
REVISION_PROMPT_VERSION = "architecture-revise-v2"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class DecisionOrigin(StrEnum):
    REQUIRED_BY_CLIENT = "required_by_client"
    PREFERRED_BY_CLIENT = "preferred_by_client"
    RECOMMENDED_BY_AI = "recommended_by_ai"
    ASSUMED = "assumed"
    REQUIRES_CLARIFICATION = "requires_clarification"


class CoverageStatus(StrEnum):
    ADDRESSED = "addressed"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"


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
    COVERAGE = "coverage"
    GRAPH = "graph"
    OUTPUT_SIZE = "output_size"


# Fixed error types raised by the custom validators below (instead of free-text messages).
EVIDENCE_REQUIRED = "evidence_required"
DUPLICATE_COMPONENT_ID = "duplicate_component_id"
DUPLICATE_ELEMENT_ID = "duplicate_element_id"
MISSING_ELEMENT_ID = "missing_element_id"
GAP_REQUIRED = "gap_required"
RELATED_REQUIRED = "related_required"
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


class InvalidCoverageError(ArchitectureError):
    code = "invalid_coverage"


class ContextTooLargeError(ArchitectureError):
    code = "context_too_large"


Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
Ref = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
ReqIds = Annotated[list[uuid.UUID], Field(max_length=50)]
TextList = Annotated[list[Text], Field(max_length=10)]
# Chunk ids of cited document passages. The model writes request-scoped aliases (P001, ...);
# they are resolved to chunk ids before validation and storage.
PassageIds = Annotated[list[uuid.UUID], Field(max_length=5)]
UNKNOWN_PASSAGE_REFERENCE = "unknown_passage_reference"
DUPLICATE_PASSAGE_SOURCE = "duplicate_passage_source"
MAX_PASSAGE_SOURCES = 60
# Fields only the service may write; model output containing them is refused.
RESERVED_KEYS = ("source_passages", "retrieval")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PassageSource(_Strict):
    """Where a cited, unconfirmed passage came from. The document text is immutable, so the
    document id and offsets stay valid even if the chunk index is rebuilt later."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: Annotated[str, StringConstraints(max_length=500)]
    source_start: Annotated[int, Field(ge=0)]
    source_end: Annotated[int, Field(ge=0)]


RetrievalStatus = Literal["used", "no_matches", "not_indexed", "no_documents", "disabled"]


class RetrievalInfo(_Strict):
    """What document context the generation request was given (never the text itself)."""

    status: RetrievalStatus
    passages_provided: Annotated[int, Field(ge=0)]
    unindexed_documents: Annotated[int, Field(ge=0)]
    budget_chars: Annotated[int, Field(ge=0)]
    truncated: bool


@dataclass(frozen=True)
class PassageCatalog:
    """The passages offered in one generation request, keyed by request-scoped alias."""

    sources: Mapping[str, PassageSource]
    info: RetrievalInfo


class Decision(_Strict):
    """A major architecture decision. `origin` says who stands behind it."""

    # Optional so proposals stored before the coverage matrix still load.
    id: Ref | None = None
    name: Short
    decision: Text
    rationale: Text
    origin: DecisionOrigin
    supporting_requirement_ids: ReqIds = Field(default_factory=list)
    source_passage_ids: PassageIds = Field(default_factory=list)
    alternatives_considered: TextList = Field(default_factory=list)
    assumptions: TextList = Field(default_factory=list)
    risks: TextList = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_matches_origin(self) -> "Decision":
        # A client requirement or an AI recommendation must point at confirmed evidence;
        # only assumptions and open points may stand without it.
        needs_evidence = self.origin in (
            DecisionOrigin.REQUIRED_BY_CLIENT,
            DecisionOrigin.PREFERRED_BY_CLIENT,
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
    source_passage_ids: PassageIds = Field(default_factory=list)


class Risk(_Strict):
    description: Text
    severity: Severity
    mitigation: Text
    requirement_ids: ReqIds = Field(default_factory=list)
    source_passage_ids: PassageIds = Field(default_factory=list)


class Assumption(_Strict):
    statement: Text
    requirement_ids: ReqIds = Field(default_factory=list)
    source_passage_ids: PassageIds = Field(default_factory=list)


class OpenQuestion(_Strict):
    question: Text
    why_it_matters: Text | None = None
    requirement_ids: ReqIds = Field(default_factory=list)
    source_passage_ids: PassageIds = Field(default_factory=list)


class Alternative(_Strict):
    title: Short
    description: Text
    trade_offs: Text
    requirement_ids: ReqIds = Field(default_factory=list)
    source_passage_ids: PassageIds = Field(default_factory=list)


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


class CoverageEntry(_Strict):
    """One confirmed requirement mapped to the design. Semantic coverage still needs a human."""

    requirement_id: uuid.UUID
    related_ids: Annotated[list[Ref], Field(max_length=10)] = Field(default_factory=list)
    verification: Text
    status: CoverageStatus
    gap: Text | None = None

    @model_validator(mode="after")
    def _status_is_consistent(self) -> "CoverageEntry":
        if self.status is not CoverageStatus.UNRESOLVED and not self.related_ids:
            raise PydanticCustomError(
                RELATED_REQUIRED, "addressed or partial coverage names a component or decision"
            )
        if self.status is not CoverageStatus.ADDRESSED and not self.gap:
            raise PydanticCustomError(GAP_REQUIRED, "partial or unresolved coverage states a gap")
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
    # None means "not recorded" (proposals created before the matrix existed).
    coverage_matrix: Annotated[list[CoverageEntry], Field(max_length=200)] | None = None
    # Provenance of cited document passages and of the retrieval step. Written by the service,
    # never by the model. Empty / None for proposals created before Phase 7C.
    source_passages: Annotated[list[PassageSource], Field(max_length=MAX_PASSAGE_SOURCES)] = Field(
        default_factory=list
    )
    retrieval: RetrievalInfo | None = None

    @model_validator(mode="after")
    def _component_ids_unique(self) -> "ArchitectureContent":
        ids = [c.id for c in self.components]
        if len(set(ids)) != len(ids):
            raise PydanticCustomError(DUPLICATE_COMPONENT_ID, "component ids must be unique")
        return self

    @model_validator(mode="after")
    def _passage_citations_have_sources(self) -> "ArchitectureContent":
        sources = [p.chunk_id for p in self.source_passages]
        if len(set(sources)) != len(sources):
            raise PydanticCustomError(DUPLICATE_PASSAGE_SOURCE, "passage sources must be unique")
        if not cited_passage_ids(self) <= set(sources):
            raise PydanticCustomError(
                UNKNOWN_PASSAGE_REFERENCE, "an item cites a passage that has no recorded source"
            )
        return self


def _decisions(content: ArchitectureContent) -> Iterable[Decision]:
    for group in (
        content.recommended_stack,
        content.integrations,
        content.data_architecture,
        content.security_architecture,
        content.non_functional_design,
    ):
        yield from group


class GeneratedArchitecture(ArchitectureContent):
    title: Short
    summary: Long
    coverage_matrix: Annotated[list[CoverageEntry], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def _decision_ids(self) -> "GeneratedArchitecture":
        ids = [d.id for d in _decisions(self)]
        if any(i is None for i in ids):
            raise PydanticCustomError(MISSING_ELEMENT_ID, "every decision needs an id")
        everything = [*ids, *(c.id for c in self.components)]
        if len(set(everything)) != len(everything):
            raise PydanticCustomError(
                DUPLICATE_ELEMENT_ID, "component and decision ids must be unique"
            )
        return self


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
    for coverage in content.coverage_matrix or []:
        yield [coverage.requirement_id]
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


def cited_passage_ids(content: ArchitectureContent) -> set[uuid.UUID]:
    cited: set[uuid.UUID] = set()
    for decision in _decisions(content):
        cited.update(decision.source_passage_ids)
    for section in (
        content.components,
        content.risks,
        content.assumptions,
        content.open_questions,
        content.alternatives,
    ):
        for entry in section:
            cited.update(entry.source_passage_ids)
    return cited


def referenced_requirement_ids(content: ArchitectureContent) -> set[uuid.UUID]:
    return {rid for ids in _id_lists(content) for rid in ids}


def verify_references(
    content: ArchitectureContent, confirmed: set[uuid.UUID], *, require_complete: bool = True
) -> None:
    """Every cited requirement must exist in this project and be confirmed.

    A recorded coverage matrix must also list each confirmed requirement exactly once (unless
    `require_complete` is off, for edits made after new requirements were confirmed) and only
    point at components or decisions of this proposal. This checks structure, not meaning.
    """
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
    verify_coverage(content, confirmed, require_complete=require_complete)


def verify_coverage(
    content: ArchitectureContent, confirmed: set[uuid.UUID], *, require_complete: bool
) -> None:
    matrix = content.coverage_matrix
    if matrix is None:
        return
    listed = Counter(entry.requirement_id for entry in matrix)
    known = {c.id for c in content.components} | {
        d.id for d in _decisions(content) if d.id is not None
    }
    duplicates = sum(1 for n in listed.values() if n > 1)
    missing = len(confirmed - set(listed)) if require_complete else 0
    unknown_related = sum(1 for e in matrix for rid in e.related_ids if rid not in known)
    if duplicates or missing or unknown_related:
        raise InvalidCoverageError(
            "The coverage matrix must list every confirmed requirement exactly once "
            "and only reference components or decisions of this proposal",
            category=FailureCategory.COVERAGE,
            diagnostics={
                "entries": len(matrix),
                "duplicate_requirements": duplicates,
                "missing_requirements": missing,
                "unknown_related_ids": unknown_related,
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
    CoverageEntry,
    PassageSource,
    RetrievalInfo,
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
    raw: str,
    *,
    finish_reason: str | None = None,
    max_chars: int | None = None,
    aliases: Mapping[str, uuid.UUID] | None = None,
    passages: PassageCatalog | None = None,
) -> GeneratedArchitecture:
    """Strictly parse model output. Failures carry content-free diagnostics; nothing is repaired.

    With `aliases`, requirement references must be this request's aliases and are resolved to
    UUIDs before schema validation; without it, references must already be UUIDs.
    """
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
    if isinstance(data, dict) and any(key in data for key in RESERVED_KEYS):
        raise _invalid(FailureCategory.SCHEMA, {**base, "reason": "reserved_field"})
    if aliases is not None:
        data = resolve_aliases(data, aliases)
    if passages is not None:
        data = resolve_passage_aliases(data, passages)
    try:
        return GeneratedArchitecture.model_validate(data)
    except ValidationError as exc:
        summary = summarize_validation_errors(exc)
        types = {e["type"] for e in summary["errors"]}
        graph_only = bool(types) and summary["errors_omitted"] == 0 and types <= GRAPH_CODES
        category = FailureCategory.GRAPH if graph_only else FailureCategory.SCHEMA
        raise _invalid(category, {**base, **summary}) from None


_DECISION_GROUPS = (
    "recommended_stack",
    "integrations",
    "data_architecture",
    "security_architecture",
    "non_functional_design",
)
_REQUIREMENT_SECTIONS = (
    "components",
    "risks",
    "assumptions",
    "open_questions",
    "alternatives",
    "implementation_phases",
)
# Phases may cite requirements but not passages.
_PASSAGE_SECTIONS = tuple(s for s in _REQUIREMENT_SECTIONS if s != "implementation_phases")


def build_alias_map(requirement_ids: Iterable[uuid.UUID]) -> dict[str, uuid.UUID]:
    """R001, R002, ... for one request, ordered by UUID so query order never matters."""
    ordered = sorted(set(requirement_ids), key=str)
    width = max(3, len(str(len(ordered))))
    return {f"R{n:0{width}d}": rid for n, rid in enumerate(ordered, start=1)}


def _alias_failure_reason(value: object, aliases: Mapping[str, uuid.UUID]) -> str | None:
    if isinstance(value, str) and value in aliases:
        return None
    if not isinstance(value, str):
        return "not_a_string"
    try:
        uuid.UUID(value)
    except ValueError:
        return "unknown_alias"
    return "uuid_instead_of_alias"


def resolve_aliases(data: Any, aliases: Mapping[str, uuid.UUID]) -> Any:
    """Replace requirement aliases with the original UUIDs in schema-known reference fields.

    Exact matches against this request's map only: no case folding, trimming or guessing. Component
    and decision ids (`related_ids`, diagram ids) are a separate namespace and are never touched.
    Malformed shapes are left for the schema validation to reject.
    """
    if not isinstance(data, dict):
        return data
    resolved = copy.deepcopy(data)
    problems: list[dict[str, str]] = []

    def one(value: object, path: str) -> str | None:
        reason = _alias_failure_reason(value, aliases)
        if reason is None:
            return str(aliases[str(value)])
        problems.append({"path": path, "reason": reason})
        return None

    def many(holder: dict[str, Any], key: str, path: str) -> None:
        values = holder.get(key)
        if not isinstance(values, list):
            return
        holder[key] = [
            out if (out := one(v, f"{path}.{key}[{i}]")) is not None else v
            for i, v in enumerate(values)
        ]

    for section in (*_DECISION_GROUPS, *_REQUIREMENT_SECTIONS):
        items = resolved.get(section)
        key = "supporting_requirement_ids" if section in _DECISION_GROUPS else "requirement_ids"
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    many(item, key, f"{section}[{index}]")
    matrix = resolved.get("coverage_matrix")
    if isinstance(matrix, list):
        for index, entry in enumerate(matrix):
            if isinstance(entry, dict) and "requirement_id" in entry:
                out = one(entry["requirement_id"], f"coverage_matrix[{index}].requirement_id")
                if out is not None:
                    entry["requirement_id"] = out

    if problems:
        raise FabricatedReferenceError(
            "The proposal uses requirement references that are not aliases of this request",
            category=FailureCategory.REFERENCE,
            diagnostics={
                "invalid_references": len(problems),
                "locations": problems[:MAX_LOGGED_ERRORS],
                "locations_omitted": max(0, len(problems) - MAX_LOGGED_ERRORS),
                "aliases_available": len(aliases),
            },
        )
    return resolved


def resolve_passage_aliases(data: Any, catalog: PassageCatalog) -> Any:
    """Replace passage aliases (P001, ...) with chunk ids and record the cited sources.

    Exact matches against this request's catalog only. Requirement aliases and component or
    decision ids are different namespaces and never match here. Citing is optional.
    """
    if not isinstance(data, dict):
        return data
    resolved = copy.deepcopy(data)
    problems: list[dict[str, str]] = []
    cited: set[str] = set()

    def many(holder: dict[str, Any], path: str) -> None:
        values = holder.get("source_passage_ids")
        if not isinstance(values, list):
            return  # absent is fine; a wrong shape is left for the schema validation
        out: list[Any] = []
        for i, value in enumerate(values):
            if isinstance(value, str) and value in catalog.sources:
                cited.add(value)
                out.append(str(catalog.sources[value].chunk_id))
                continue
            reason = "not_a_string" if not isinstance(value, str) else "unknown_alias"
            problems.append({"path": f"{path}.source_passage_ids[{i}]", "reason": reason})
            out.append(value)
        holder["source_passage_ids"] = out

    for section in (*_DECISION_GROUPS, *_PASSAGE_SECTIONS):
        items = resolved.get(section)
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict):
                    many(item, f"{section}[{index}]")
    if problems:
        raise FabricatedReferenceError(
            "The proposal cites passages that are not aliases of this request",
            category=FailureCategory.REFERENCE,
            diagnostics={
                "namespace": "passage",
                "invalid_references": len(problems),
                "locations": problems[:MAX_LOGGED_ERRORS],
                "locations_omitted": max(0, len(problems) - MAX_LOGGED_ERRORS),
                "aliases_available": len(catalog.sources),
            },
        )
    resolved["source_passages"] = [
        catalog.sources[alias].model_dump(mode="json") for alias in sorted(cited)
    ]
    resolved["retrieval"] = catalog.info.model_dump(mode="json")
    return resolved


def content_to_json(content: ArchitectureContent) -> dict[str, Any]:
    data: dict[str, Any] = content.model_dump(mode="json", exclude={"title", "summary"})
    return data
