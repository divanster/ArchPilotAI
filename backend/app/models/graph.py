import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.project import _utcnow

NODE_KINDS = (
    "('requirement', 'component', 'decision', 'risk', 'assumption', 'open_question', 'passage')"
)
EDGE_KINDS = (
    "('covered_by', 'justified_by', 'depends_on', 'affects', 'supported_by', 'raises_question')"
)


class GraphNode(Base):
    """Derived from a stored proposal; rebuildable and never a source of truth."""

    __tablename__ = "graph_nodes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "project_id"],
            ["architecture_proposals.id", "architecture_proposals.project_id"],
            ondelete="CASCADE",
            name="fk_graph_node_proposal",
        ),
        # Target of the composite foreign keys on graph_edges.
        UniqueConstraint("id", "project_id", "proposal_id", name="uq_graph_node_scope"),
        UniqueConstraint("proposal_id", "kind", "source_ref", name="uq_graph_node_ref"),
        CheckConstraint(f"kind IN {NODE_KINDS}", name="ck_graph_node_kind"),
        Index("ix_graph_nodes_proposal", "proposal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    proposal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "project_id"],
            ["architecture_proposals.id", "architecture_proposals.project_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_proposal",
        ),
        # Both ends must be nodes of the same project and the same proposal.
        ForeignKeyConstraint(
            ["source_id", "project_id", "proposal_id"],
            ["graph_nodes.id", "graph_nodes.project_id", "graph_nodes.proposal_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_source",
        ),
        ForeignKeyConstraint(
            ["target_id", "project_id", "proposal_id"],
            ["graph_nodes.id", "graph_nodes.project_id", "graph_nodes.proposal_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_target",
        ),
        UniqueConstraint("proposal_id", "source_id", "target_id", "kind", name="uq_graph_edge"),
        CheckConstraint(f"kind IN {EDGE_KINDS}", name="ck_graph_edge_kind"),
        Index("ix_graph_edges_proposal", "proposal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    proposal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # The stored field the edge was read from (a path); never model text.
    evidence: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
