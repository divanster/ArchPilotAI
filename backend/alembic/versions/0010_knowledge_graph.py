"""derived knowledge graph tables (additive; existing proposals simply have no graph yet)

Revision ID: 0010_knowledge_graph
Revises: 0009_revision_outcome
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_knowledge_graph"
down_revision: str | None = "0009_revision_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NODE_KINDS = "('requirement', 'component', 'decision', 'risk', 'assumption', 'open_question', 'passage')"
EDGE_KINDS = (
    "('covered_by', 'justified_by', 'depends_on', 'affects', 'supported_by', 'raises_question')"
)


def upgrade() -> None:
    with op.batch_alter_table("architecture_proposals") as batch:
        batch.create_unique_constraint("uq_architecture_id_project", ["id", "project_id"])

    op.create_table(
        "graph_nodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id", "project_id"],
            ["architecture_proposals.id", "architecture_proposals.project_id"],
            ondelete="CASCADE",
            name="fk_graph_node_proposal",
        ),
        sa.UniqueConstraint("id", "project_id", "proposal_id", name="uq_graph_node_scope"),
        sa.UniqueConstraint("proposal_id", "kind", "source_ref", name="uq_graph_node_ref"),
        sa.CheckConstraint(f"kind IN {NODE_KINDS}", name="ck_graph_node_kind"),
    )
    op.create_index("ix_graph_nodes_proposal", "graph_nodes", ["proposal_id"])

    op.create_table(
        "graph_edges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("evidence", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id", "project_id"],
            ["architecture_proposals.id", "architecture_proposals.project_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_proposal",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "project_id", "proposal_id"],
            ["graph_nodes.id", "graph_nodes.project_id", "graph_nodes.proposal_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_source",
        ),
        sa.ForeignKeyConstraint(
            ["target_id", "project_id", "proposal_id"],
            ["graph_nodes.id", "graph_nodes.project_id", "graph_nodes.proposal_id"],
            ondelete="CASCADE",
            name="fk_graph_edge_target",
        ),
        sa.UniqueConstraint("proposal_id", "source_id", "target_id", "kind", name="uq_graph_edge"),
        sa.CheckConstraint(f"kind IN {EDGE_KINDS}", name="ck_graph_edge_kind"),
    )
    op.create_index("ix_graph_edges_proposal", "graph_edges", ["proposal_id"])


def downgrade() -> None:
    op.drop_index("ix_graph_edges_proposal", table_name="graph_edges")
    op.drop_table("graph_edges")
    op.drop_index("ix_graph_nodes_proposal", table_name="graph_nodes")
    op.drop_table("graph_nodes")
    with op.batch_alter_table("architecture_proposals") as batch:
        batch.drop_constraint("uq_architecture_id_project", type_="unique")
