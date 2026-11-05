"""change requests with architecture versioning and plan deltas (additive)

Revision ID: 0024_change_requests
Revises: 0023_task_checklists
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_change_requests"
down_revision: str | None = "0023_task_checklists"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "('draft', 'analysed', 'approved', 'in_progress', 'done', 'rejected')"
CLASSES = "('code_only', 'architecture')"
LINKED = (
    ("architecture_proposals", "fk_architecture_change_request"),
    ("implementation_plans", "fk_plan_change_request"),
    ("agent_runs", "fk_agent_run_change_request"),
)


def upgrade() -> None:
    op.create_table(
        "change_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("author", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("impact_class", sa.String(20), nullable=True),
        sa.Column("impact", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("class_override", sa.String(20), nullable=True),
        sa.Column("overridden_by", sa.String(100), nullable=True),
        sa.Column("override_reason", sa.String(1000), nullable=True),
        sa.Column("baseline", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("drift", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("proposed_requirement_ids", sa.JSON(), nullable=False),
        sa.Column("removals_confirmed", sa.JSON(), nullable=False),
        sa.Column("pull_request_url", sa.String(500), nullable=True),
        sa.Column("decided_by", sa.String(100), nullable=True),
        sa.Column("decision_reason", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "key", name="uq_change_request_key"),
        sa.UniqueConstraint("id", "project_id", name="uq_change_request_id_project"),
        sa.CheckConstraint(f"status IN {STATUSES}", name="ck_change_request_status"),
        sa.CheckConstraint(
            f"impact_class IS NULL OR impact_class IN {CLASSES}", name="ck_change_request_class"
        ),
        sa.CheckConstraint(
            f"class_override IS NULL OR class_override IN {CLASSES}",
            name="ck_change_request_override",
        ),
    )
    op.create_index(
        "ix_change_requests_project_status", "change_requests", ["project_id", "status"]
    )
    for table, fk in LINKED:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("change_request_id", sa.Uuid(), nullable=True))
            batch.create_foreign_key(
                fk, "change_requests", ["change_request_id"], ["id"], ondelete="SET NULL"
            )
    with op.batch_alter_table("implementation_plans") as batch:
        batch.add_column(
            sa.Column("kind", sa.String(10), nullable=False, server_default="full")
        )
        batch.add_column(sa.Column("base_plan_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("implementation_plans") as batch:
        batch.drop_column("base_plan_id")
        batch.drop_column("kind")
    for table, fk in reversed(LINKED):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(fk, type_="foreignkey")
            batch.drop_column("change_request_id")
    op.drop_index("ix_change_requests_project_status", table_name="change_requests")
    op.drop_table("change_requests")
