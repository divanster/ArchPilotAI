import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict

from app.api.ai import require_non_production
from app.api.deps import GraphServiceDep
from app.domain.graph import DEFAULT_IMPACT_DEPTH, MAX_IMPACT_DEPTH, ImpactMode
from app.services.architecture import ProposalNotFoundError
from app.services.graph import GraphNodeNotFoundError
from app.services.project import ProjectNotFoundError

router = APIRouter(
    prefix="/api/projects/{project_id}/architecture/{proposal_id}/graph",
    tags=["graph"],
    dependencies=[Depends(require_non_production)],  # no authentication exists yet
)

NotFound = (ProjectNotFoundError, ProposalNotFoundError, GraphNodeNotFoundError)


class GraphNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    label: str
    source_ref: str


class GraphEdgeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_id: uuid.UUID
    target_id: uuid.UUID
    kind: str
    evidence: str


class GraphResponse(BaseModel):
    built: bool
    built_at: datetime | None
    node_count: int
    edge_count: int
    truncated: bool
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]


class RebuildResponse(BaseModel):
    node_count: int
    edge_count: int
    skipped_requirement_refs: int
    unmapped_nodes: list[str] = []


class ImpactItem(BaseModel):
    node: GraphNodeOut
    depth: int
    via_kind: str
    via_evidence: str
    reached_from: uuid.UUID
    via_hub: bool


class HubOut(BaseModel):
    node: GraphNodeOut
    degree: int


class ImpactResponse(BaseModel):
    root: GraphNodeOut
    mode: str
    max_depth: int
    hub_threshold: int
    truncated: bool
    items: list[ImpactItem]
    hubs_not_expanded: list[HubOut]


class OrphansResponse(BaseModel):
    orphans: list[GraphNodeOut]


@router.get("", response_model=GraphResponse)
def get_graph(
    project_id: uuid.UUID, proposal_id: uuid.UUID, service: GraphServiceDep
) -> GraphResponse:
    try:
        snap = service.get(project_id, proposal_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return GraphResponse(
        built=snap.built,
        built_at=snap.built_at,
        node_count=snap.node_count,
        edge_count=snap.edge_count,
        truncated=snap.truncated,
        nodes=[GraphNodeOut.model_validate(n) for n in snap.nodes],
        edges=[GraphEdgeOut.model_validate(e) for e in snap.edges],
    )


@router.post("/rebuild", response_model=RebuildResponse)
def rebuild_graph(
    project_id: uuid.UUID, proposal_id: uuid.UUID, service: GraphServiceDep
) -> RebuildResponse:
    try:
        summary = service.build(project_id, proposal_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return RebuildResponse(
        node_count=summary.node_count,
        edge_count=summary.edge_count,
        skipped_requirement_refs=summary.skipped_requirement_refs,
        unmapped_nodes=list(summary.unmapped_nodes),
    )


@router.get("/impact", response_model=ImpactResponse)
def graph_impact(
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    service: GraphServiceDep,
    node_id: uuid.UUID,
    depth: Annotated[int, Query(ge=1, le=MAX_IMPACT_DEPTH)] = DEFAULT_IMPACT_DEPTH,
    mode: ImpactMode = ImpactMode.DIRECT,
) -> ImpactResponse:
    try:
        view = service.impact(project_id, proposal_id, node_id, depth, mode)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return ImpactResponse(
        root=GraphNodeOut.model_validate(view.root),
        mode=view.mode.value,
        max_depth=view.max_depth,
        hub_threshold=view.hub_threshold,
        truncated=view.result.truncated,
        hubs_not_expanded=[
            HubOut(node=GraphNodeOut.model_validate(view.nodes[h.node_id]), degree=h.degree)
            for h in view.result.hubs
        ],
        items=[
            ImpactItem(
                node=GraphNodeOut.model_validate(view.nodes[h.node_id]),
                depth=h.depth,
                via_kind=h.via_kind.value,
                via_evidence=h.via_evidence,
                reached_from=h.reached_from,
                via_hub=h.via_hub,
            )
            for h in view.result.hits
        ],
    )


@router.get("/orphans", response_model=OrphansResponse)
def graph_orphans(
    project_id: uuid.UUID, proposal_id: uuid.UUID, service: GraphServiceDep
) -> OrphansResponse:
    try:
        found = service.orphans(project_id, proposal_id)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return OrphansResponse(orphans=[GraphNodeOut.model_validate(n) for n in found])
