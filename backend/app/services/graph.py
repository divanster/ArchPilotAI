import uuid
from dataclasses import dataclass
from datetime import datetime

from app.domain.architecture import ArchitectureContent
from app.domain.graph import (
    DEFAULT_HUB_THRESHOLD,
    DEFAULT_IMPACT_DEPTH,
    MAX_GRAPH_EDGES,
    MAX_GRAPH_NODES,
    EdgeKind,
    EdgeSpec,
    ImpactMode,
    ImpactResult,
    NodeKind,
    NodeSpec,
    build_graph,
    impact,
    orphan_requirements,
)
from app.models.graph import GraphEdge, GraphNode
from app.repositories.architecture import ArchitectureRepository
from app.repositories.graph import GraphRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture import ProposalNotFoundError
from app.services.project import ProjectNotFoundError


class GraphNodeNotFoundError(Exception):
    def __init__(self, node_id: uuid.UUID) -> None:
        super().__init__(f"Graph node {node_id} not found")


@dataclass(frozen=True)
class GraphSnapshot:
    built: bool
    built_at: datetime | None
    node_count: int
    edge_count: int
    truncated: bool
    nodes: list[GraphNode]
    edges: list[GraphEdge]


@dataclass(frozen=True)
class RebuildSummary:
    node_count: int
    edge_count: int
    skipped_requirement_refs: int
    unmapped_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImpactView:
    root: GraphNode
    result: ImpactResult
    nodes: dict[uuid.UUID, GraphNode]
    max_depth: int
    mode: ImpactMode
    hub_threshold: int


def _node_spec(n: GraphNode) -> NodeSpec:
    return NodeSpec(n.id, NodeKind(n.kind), n.label, n.source_ref)


def _edge_spec(e: GraphEdge) -> EdgeSpec:
    return EdgeSpec(e.id, e.source_id, e.target_id, EdgeKind(e.kind), e.evidence)


class GraphService:
    """The graph is derived from the stored proposal; reads never change it."""

    def __init__(
        self,
        graph: GraphRepository,
        architecture: ArchitectureRepository,
        projects: ProjectRepository,
        requirements: RequirementRepository,
        hub_threshold: int = DEFAULT_HUB_THRESHOLD,
    ) -> None:
        self._hub_threshold = hub_threshold
        self._graph = graph
        self._architecture = architecture
        self._projects = projects
        self._requirements = requirements

    def _proposal(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        if self._architecture.get(project_id, proposal_id) is None:
            raise ProposalNotFoundError(proposal_id)

    def build(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> RebuildSummary:
        """Rebuild from the stored proposal and the currently confirmed requirements."""
        self._proposal(project_id, proposal_id)
        proposal = self._architecture.get(project_id, proposal_id)
        assert proposal is not None
        confirmed = {r.id: r.title for r in self._requirements.list(project_id, "confirmed")}
        spec = build_graph(
            proposal.id, ArchitectureContent.model_validate(proposal.content), confirmed
        )
        self._graph.replace(project_id, proposal_id, spec.nodes, spec.edges)
        return RebuildSummary(
            len(spec.nodes),
            len(spec.edges),
            spec.skipped_requirement_refs,
            tuple(spec.unmapped_nodes),
        )

    def get(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> GraphSnapshot:
        self._proposal(project_id, proposal_id)
        nodes = list(self._graph.nodes(project_id, proposal_id))
        if not nodes:
            return GraphSnapshot(False, None, 0, 0, False, [], [])
        edges = list(self._graph.edges(project_id, proposal_id))
        built_at = self._graph.built_at(project_id, proposal_id)
        return GraphSnapshot(
            True,
            built_at if isinstance(built_at, datetime) else None,
            len(nodes),
            len(edges),
            len(nodes) > MAX_GRAPH_NODES or len(edges) > MAX_GRAPH_EDGES,
            nodes[:MAX_GRAPH_NODES],
            edges[:MAX_GRAPH_EDGES],
        )

    def impact(
        self,
        project_id: uuid.UUID,
        proposal_id: uuid.UUID,
        node_id: uuid.UUID,
        depth: int = DEFAULT_IMPACT_DEPTH,
        mode: ImpactMode = ImpactMode.DIRECT,
    ) -> ImpactView:
        self._proposal(project_id, proposal_id)
        nodes = {n.id: n for n in self._graph.nodes(project_id, proposal_id)}
        if node_id not in nodes:
            raise GraphNodeNotFoundError(node_id)
        edges = [_edge_spec(e) for e in self._graph.edges(project_id, proposal_id)]
        result = impact(
            node_id, edges, mode=mode, max_depth=depth, hub_threshold=self._hub_threshold
        )
        shown = 1 if mode is ImpactMode.DIRECT else min(max(depth, 1), 5)
        return ImpactView(nodes[node_id], result, nodes, shown, mode, self._hub_threshold)

    def orphans(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> list[GraphNode]:
        self._proposal(project_id, proposal_id)
        nodes = {n.id: n for n in self._graph.nodes(project_id, proposal_id)}
        edges = [_edge_spec(e) for e in self._graph.edges(project_id, proposal_id)]
        found = orphan_requirements([_node_spec(n) for n in nodes.values()], edges)
        return [nodes[n.id] for n in found]
