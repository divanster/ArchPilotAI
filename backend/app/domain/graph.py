"""Traceability graph derived from a stored proposal. Pure and deterministic: no database, no LLM.

The graph is derived data and never a source of truth. Every node and edge comes from a stored
field of the proposal (or a confirmed requirement); nothing is inferred. Node ids and edge ids are
uuid5 values of the proposal id and the stored reference, so rebuilding gives identical rows.

Edge direction is "source depends on target". Kinds:
  covered_by       requirement  -> component | decision   coverage_matrix[i].related_ids
  justified_by     decision | component -> requirement    *requirement_ids fields
  depends_on       component    -> component              diagram.edges[i] (nodes mapped by name)
  affects          risk         -> requirement            risks[i].requirement_ids
  supported_by     assumption   -> requirement            assumptions[i].requirement_ids
                   any cited item -> passage              <path>.source_passage_ids
  raises_question  open_question -> requirement           open_questions[i].requirement_ids
Alternatives and implementation phases are not graph nodes.
"""

import uuid
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.architecture import ArchitectureContent

LABEL_MAX = 200
MAX_IMPACT_DEPTH = 5
DEFAULT_IMPACT_DEPTH = 3
MAX_IMPACT_RESULTS = 200
DEFAULT_HUB_THRESHOLD = 5
MAX_GRAPH_NODES = 1000
MAX_GRAPH_EDGES = 3000

_DECISION_GROUPS = (
    "recommended_stack",
    "integrations",
    "data_architecture",
    "security_architecture",
    "non_functional_design",
)


class ImpactMode(StrEnum):
    DIRECT = "direct"
    TRANSITIVE = "transitive"


class NodeKind(StrEnum):
    REQUIREMENT = "requirement"
    COMPONENT = "component"
    DECISION = "decision"
    RISK = "risk"
    ASSUMPTION = "assumption"
    OPEN_QUESTION = "open_question"
    PASSAGE = "passage"


class EdgeKind(StrEnum):
    COVERED_BY = "covered_by"
    JUSTIFIED_BY = "justified_by"
    DEPENDS_ON = "depends_on"
    AFFECTS = "affects"
    SUPPORTED_BY = "supported_by"
    RAISES_QUESTION = "raises_question"


@dataclass(frozen=True)
class NodeSpec:
    id: uuid.UUID
    kind: NodeKind
    label: str
    source_ref: str  # requirement or chunk UUID, or the item's path in the proposal


@dataclass(frozen=True)
class EdgeSpec:
    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    kind: EdgeKind
    evidence: str  # the stored field the edge was read from; never model text


@dataclass(frozen=True)
class GraphSpec:
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    # References to requirements that are not (or no longer) confirmed, left out of the graph.
    skipped_requirement_refs: int
    # Names of diagram nodes with no component of exactly that name; they add no edges.
    unmapped_nodes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiagramMapping:
    components: dict[str, int]  # diagram node id -> index into content.components
    unmapped: list[str]  # names of the diagram nodes that map to no component


def map_diagram_nodes(content: ArchitectureContent) -> DiagramMapping:
    """Diagram node ids (n-*) and component ids (c-*) are different namespaces, so a node is
    mapped to a component by exact name only. A name shared by several components is ambiguous
    and maps to nothing. Nothing is guessed."""
    names = Counter(c.name for c in content.components)
    by_name = {c.name: i for i, c in enumerate(content.components) if names[c.name] == 1}
    mapped = {n.id: by_name[n.name] for n in content.diagram.nodes if n.name in by_name}
    unmapped = [n.name for n in content.diagram.nodes if n.id not in mapped]
    return DiagramMapping(mapped, unmapped)


def _label(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= LABEL_MAX else flat[: LABEL_MAX - 1] + "…"


def node_id(proposal_id: uuid.UUID, kind: NodeKind, source_ref: str) -> uuid.UUID:
    return uuid.uuid5(proposal_id, f"node:{kind.value}:{source_ref}")


def edge_id(
    proposal_id: uuid.UUID, source: uuid.UUID, target: uuid.UUID, kind: EdgeKind
) -> uuid.UUID:
    return uuid.uuid5(proposal_id, f"edge:{kind.value}:{source}:{target}")


def build_graph(
    proposal_id: uuid.UUID,
    content: ArchitectureContent,
    confirmed: Mapping[uuid.UUID, str],
) -> GraphSpec:
    """`confirmed` maps each currently confirmed requirement id to its title."""
    nodes: dict[uuid.UUID, NodeSpec] = {}
    edges: dict[tuple[uuid.UUID, uuid.UUID, EdgeKind], EdgeSpec] = {}
    skipped = 0

    def add_node(kind: NodeKind, ref: str, label: str) -> uuid.UUID:
        nid = node_id(proposal_id, kind, ref)
        nodes.setdefault(nid, NodeSpec(nid, kind, _label(label), ref))
        return nid

    def add_edge(source: uuid.UUID, target: uuid.UUID, kind: EdgeKind, evidence: str) -> None:
        key = (source, target, kind)
        if key not in edges:
            edges[key] = EdgeSpec(
                edge_id(proposal_id, source, target, kind), source, target, kind, evidence
            )

    requirement_nodes: dict[uuid.UUID, uuid.UUID] = {}
    for rid in sorted(confirmed, key=str):
        requirement_nodes[rid] = add_node(NodeKind.REQUIREMENT, str(rid), confirmed[rid])

    def link_requirements(
        source: uuid.UUID, ids: Iterable[uuid.UUID], kind: EdgeKind, evidence: str
    ) -> None:
        nonlocal skipped
        for rid in ids:
            if rid in requirement_nodes:
                add_edge(source, requirement_nodes[rid], kind, evidence)
            else:
                skipped += 1

    elements: dict[str, uuid.UUID] = {}  # component / decision id -> node
    components: dict[str, uuid.UUID] = {}
    cited: list[tuple[uuid.UUID, str, Sequence[uuid.UUID]]] = []  # node, path, chunk ids

    for i, comp in enumerate(content.components):
        path = f"components[{i}]"
        nid = add_node(NodeKind.COMPONENT, path, comp.name)
        elements.setdefault(comp.id, nid)
        components.setdefault(comp.id, nid)
        link_requirements(
            nid, comp.requirement_ids, EdgeKind.JUSTIFIED_BY, f"{path}.requirement_ids"
        )
        cited.append((nid, path, comp.source_passage_ids))

    for group in _DECISION_GROUPS:
        for i, decision in enumerate(getattr(content, group)):
            path = f"{group}[{i}]"
            nid = add_node(NodeKind.DECISION, path, decision.name)
            if decision.id:
                elements.setdefault(decision.id, nid)
            link_requirements(
                nid,
                decision.supporting_requirement_ids,
                EdgeKind.JUSTIFIED_BY,
                f"{path}.supporting_requirement_ids",
            )
            cited.append((nid, path, decision.source_passage_ids))

    for i, risk in enumerate(content.risks):
        path = f"risks[{i}]"
        nid = add_node(NodeKind.RISK, path, risk.description)
        link_requirements(nid, risk.requirement_ids, EdgeKind.AFFECTS, f"{path}.requirement_ids")
        cited.append((nid, path, risk.source_passage_ids))

    for i, assumption in enumerate(content.assumptions):
        path = f"assumptions[{i}]"
        nid = add_node(NodeKind.ASSUMPTION, path, assumption.statement)
        link_requirements(
            nid, assumption.requirement_ids, EdgeKind.SUPPORTED_BY, f"{path}.requirement_ids"
        )
        cited.append((nid, path, assumption.source_passage_ids))

    for i, question in enumerate(content.open_questions):
        path = f"open_questions[{i}]"
        nid = add_node(NodeKind.OPEN_QUESTION, path, question.question)
        link_requirements(
            nid, question.requirement_ids, EdgeKind.RAISES_QUESTION, f"{path}.requirement_ids"
        )
        cited.append((nid, path, question.source_passage_ids))

    for i, entry in enumerate(content.coverage_matrix or []):
        if entry.requirement_id not in requirement_nodes:
            skipped += 1
            continue
        for related in entry.related_ids:
            if related in elements:
                add_edge(
                    requirement_nodes[entry.requirement_id],
                    elements[related],
                    EdgeKind.COVERED_BY,
                    f"coverage_matrix[{i}].related_ids",
                )

    mapping = map_diagram_nodes(content)
    for i, edge in enumerate(content.diagram.edges):
        src, tgt = mapping.components.get(edge.source), mapping.components.get(edge.target)
        if src is not None and tgt is not None and src != tgt:
            add_edge(
                node_id(proposal_id, NodeKind.COMPONENT, f"components[{src}]"),
                node_id(proposal_id, NodeKind.COMPONENT, f"components[{tgt}]"),
                EdgeKind.DEPENDS_ON,
                f"diagram.edges[{i}]",
            )

    sources = {p.chunk_id: p for p in content.source_passages}
    for nid, path, chunk_ids in cited:
        for chunk_id in chunk_ids:
            source = sources.get(chunk_id)
            if source is None:
                continue
            passage = add_node(
                NodeKind.PASSAGE,
                str(chunk_id),
                f"{source.filename} [{source.source_start}-{source.source_end}]",
            )
            add_edge(nid, passage, EdgeKind.SUPPORTED_BY, f"{path}.source_passage_ids")

    return GraphSpec(
        nodes=sorted(nodes.values(), key=lambda n: (n.kind.value, n.source_ref)),
        edges=sorted(
            edges.values(), key=lambda e: (e.kind.value, str(e.source_id), str(e.target_id))
        ),
        skipped_requirement_refs=skipped,
        unmapped_nodes=mapping.unmapped,
    )


@dataclass(frozen=True)
class ImpactHit:
    node_id: uuid.UUID
    depth: int
    # The edge that reached this node: it points at `reached_from`.
    via_kind: EdgeKind
    via_evidence: str
    reached_from: uuid.UUID
    # The path started at a hub (only the queried node can be expanded when it is one).
    via_hub: bool = False


@dataclass(frozen=True)
class Hub:
    node_id: uuid.UUID
    degree: int  # edges touching the node, in either direction


@dataclass(frozen=True)
class ImpactResult:
    hits: list[ImpactHit]
    truncated: bool  # more nodes lay beyond the depth cap or the result cap
    hubs: list[Hub] = field(default_factory=list)  # reached, but not expanded


def impact(
    root: uuid.UUID,
    edges: Iterable[EdgeSpec],
    *,
    mode: ImpactMode = ImpactMode.DIRECT,
    max_depth: int = DEFAULT_IMPACT_DEPTH,
    limit: int = MAX_IMPACT_RESULTS,
    hub_threshold: int = DEFAULT_HUB_THRESHOLD,
) -> ImpactResult:
    """What depends on `root`: every node with a path of edges leading to it (breadth first).

    direct: depth 1 only. transitive: up to `max_depth`, but a hub (degree above the threshold)
    is reported and not expanded, because everything it is shared with would otherwise look
    affected. The queried node is always expanded. Cycle-safe (each node is visited once, at its
    shortest depth) and capped by result count.
    """
    edge_list = list(edges)
    direct = mode is ImpactMode.DIRECT
    max_depth = 1 if direct else max(1, min(max_depth, MAX_IMPACT_DEPTH))
    incoming: dict[uuid.UUID, list[EdgeSpec]] = {}
    degree: dict[uuid.UUID, int] = {}
    for edge in edge_list:
        incoming.setdefault(edge.target_id, []).append(edge)
        degree[edge.source_id] = degree.get(edge.source_id, 0) + 1
        degree[edge.target_id] = degree.get(edge.target_id, 0) + 1
    for group in incoming.values():
        group.sort(key=lambda e: (e.kind.value, str(e.source_id), e.evidence))

    def is_hub(node: uuid.UUID) -> bool:
        return degree.get(node, 0) > hub_threshold

    seen = {root}
    queue: deque[tuple[uuid.UUID, int]] = deque([(root, 0)])
    hits: list[ImpactHit] = []
    hubs: list[Hub] = []
    truncated = False
    while queue:
        current, depth = queue.popleft()
        if not direct and current != root and is_hub(current):
            if any(e.source_id not in seen for e in incoming.get(current, [])):
                hubs.append(Hub(current, degree[current]))
            continue
        via_hub = current == root and not direct and is_hub(root)
        for edge in incoming.get(current, []):
            if edge.source_id in seen:
                continue
            if depth >= max_depth:
                truncated = truncated or not direct  # direct mode asks for depth 1 only
                continue
            if len(hits) >= limit:
                truncated = True
                continue
            seen.add(edge.source_id)
            hits.append(
                ImpactHit(edge.source_id, depth + 1, edge.kind, edge.evidence, current, via_hub)
            )
            queue.append((edge.source_id, depth + 1))
    return ImpactResult(hits, truncated, hubs)


def orphan_requirements(nodes: Iterable[NodeSpec], edges: Iterable[EdgeSpec]) -> list[NodeSpec]:
    """Requirements not connected to any component or decision, in either direction."""
    node_list = list(nodes)
    kinds = {n.id: n.kind for n in node_list}
    linked: set[uuid.UUID] = set()
    for edge in edges:
        a, b = kinds.get(edge.source_id), kinds.get(edge.target_id)
        if a is NodeKind.REQUIREMENT and b in (NodeKind.COMPONENT, NodeKind.DECISION):
            linked.add(edge.source_id)
        elif b is NodeKind.REQUIREMENT and a in (NodeKind.COMPONENT, NodeKind.DECISION):
            linked.add(edge.target_id)
    return sorted(
        (n for n in node_list if n.kind is NodeKind.REQUIREMENT and n.id not in linked),
        key=lambda n: (n.label, n.source_ref),
    )
