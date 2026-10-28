"""implementation plans (additive; existing projects simply have no plan yet)

Revision ID: 0011_implementation_plans
Revises: 0010_knowledge_graph
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_implementation_plans"
down_revision: str | None = "0010_knowledge_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_KINDS = "('setup', 'component', 'integration', 'security', 'data', 'test', 'nfr')"


def upgrade() -> None:
    op.create_table(
        "implementation_plans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_graph_built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unattached_questions", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id", "project_id"],
            ["architecture_proposals.id", "architecture_proposals.project_id"],
            ondelete="CASCADE",
            name="fk_plan_proposal",
        ),
        sa.UniqueConstraint("project_id", "version", name="uq_plan_project_version"),
        sa.UniqueConstraint("id", "project_id", name="uq_plan_id_project"),
        sa.CheckConstraint("status IN ('draft', 'approved', 'rejected')", name="ck_plan_status"),
        sa.CheckConstraint("version >= 1", name="ck_plan_version"),
    )
    op.create_index(
        "ix_plans_project_status", "implementation_plans", ["project_id", "status"]
    )

    op.create_table(
        "plan_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(200), nullable=False),
        sa.Column("component_ref", sa.String(64), nullable=True),
        sa.Column("requirement_ids", sa.JSON(), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("blocking_questions", sa.JSON(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_plan_task_plan",
        ),
        sa.UniqueConstraint("plan_id", "key", name="uq_plan_task_key"),
        sa.UniqueConstraint("plan_id", "order_index", name="uq_plan_task_order"),
        sa.CheckConstraint(f"kind IN {TASK_KINDS}", name="ck_plan_task_kind"),
        sa.CheckConstraint("status IN ('todo')", name="ck_plan_task_status"),
    )
    op.create_index("ix_plan_tasks_plan", "plan_tasks", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_plan_tasks_plan", table_name="plan_tasks")
    op.drop_table("plan_tasks")
    op.drop_index("ix_plans_project_status", table_name="implementation_plans")
    op.drop_table("implementation_plans")
