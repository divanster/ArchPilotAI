import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.domain.graph import EdgeSpec, NodeSpec
from app.models.graph import GraphEdge, GraphNode


class GraphRepository:
    """Every query is scoped by project and proposal."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def replace(
        self,
        project_id: uuid.UUID,
        proposal_id: uuid.UUID,
        nodes: Sequence[NodeSpec],
        edges: Sequence[EdgeSpec],
    ) -> None:
        """Swap the whole graph of one proposal in a single transaction."""
        try:
            self._session.execute(delete(GraphEdge).where(GraphEdge.proposal_id == proposal_id))
            self._session.execute(delete(GraphNode).where(GraphNode.proposal_id == proposal_id))
            self._session.add_all(
                GraphNode(
                    id=n.id,
                    project_id=project_id,
                    proposal_id=proposal_id,
                    kind=n.kind.value,
                    label=n.label,
                    source_ref=n.source_ref,
                )
                for n in nodes
            )
            self._session.flush()
            self._session.add_all(
                GraphEdge(
                    id=e.id,
                    project_id=project_id,
                    proposal_id=proposal_id,
                    source_id=e.source_id,
                    target_id=e.target_id,
                    kind=e.kind.value,
                    evidence=e.evidence,
                )
                for e in edges
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def nodes(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> Sequence[GraphNode]:
        stmt = (
            select(GraphNode)
            .where(GraphNode.project_id == project_id, GraphNode.proposal_id == proposal_id)
            .order_by(GraphNode.kind, GraphNode.source_ref)
        )
        return self._session.scalars(stmt).all()

    def edges(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> Sequence[GraphEdge]:
        stmt = (
            select(GraphEdge)
            .where(GraphEdge.project_id == project_id, GraphEdge.proposal_id == proposal_id)
            .order_by(GraphEdge.kind, GraphEdge.source_id, GraphEdge.target_id)
        )
        return self._session.scalars(stmt).all()

    def built_at(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> object | None:
        stmt = select(func.max(GraphNode.created_at)).where(
            GraphNode.project_id == project_id, GraphNode.proposal_id == proposal_id
        )
        return self._session.scalar(stmt)
