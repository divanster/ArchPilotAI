"""human technology decisions per project (additive)

Revision ID: 0016_project_decisions
Revises: 0015_agent_runs
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016_project_decisions"
down_revision: str | None = "0015_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AREAS = ("backend", "frontend", "database", "hosting", "auth", "testing", "style")


def upgrade() -> None:
    op.create_table(
        "project_decisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("area", sa.String(20), nullable=False),
        sa.Column("choice", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("decided_by", sa.String(100), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("id", "project_id", name="uq_decision_id_project"),
        sa.ForeignKeyConstraint(
            ["superseded_by", "project_id"],
            ["project_decisions.id", "project_decisions.project_id"],
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
            name="fk_decision_superseded_by",
        ),
        sa.CheckConstraint(
            "area IN (" + ", ".join(f"'{a}'" for a in AREAS) + ")", name="ck_decision_area"
        ),
    )
    op.create_index(
        "uq_decision_current_area",
        "project_decisions",
        ["project_id", "area"],
        unique=True,
        sqlite_where=sa.text("superseded_by IS NULL"),
        postgresql_where=sa.text("superseded_by IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_decision_current_area", table_name="project_decisions")
    op.drop_table("project_decisions")
