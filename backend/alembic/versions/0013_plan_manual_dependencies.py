"""plan dependency sources and manual dependency edit audit trail (additive)

Revision ID: 0013_plan_manual_dependencies
Revises: 0012_plan_dependency_notes
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_plan_manual_dependencies"
down_revision: str | None = "0012_plan_dependency_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("plan_tasks") as batch:
        batch.add_column(
            sa.Column(
                "dependency_sources", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
            )
        )
    op.create_table(
        "plan_dependency_edits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("task_key", sa.String(20), nullable=False),
        sa.Column("depends_on_key", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("author", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_plan_edit_plan",
        ),
        sa.CheckConstraint("action IN ('add', 'remove')", name="ck_plan_edit_action"),
    )
    op.create_index("ix_plan_edits_plan", "plan_dependency_edits", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_plan_edits_plan", table_name="plan_dependency_edits")
    op.drop_table("plan_dependency_edits")
    with op.batch_alter_table("plan_tasks") as batch:
        batch.drop_column("dependency_sources")
