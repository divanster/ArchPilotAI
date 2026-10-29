"""agent jobs: execution-provider commands of a run (additive)

Revision ID: 0018_agent_jobs
Revises: 0017_patch_feedback_coverage
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_agent_jobs"
down_revision: str | None = "0017_patch_feedback_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.String(20), nullable=False),
        sa.Column("variant", sa.String(20), nullable=False),
        sa.Column("select_test", sa.String(300), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("timeout_seconds", sa.Float(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            ondelete="CASCADE",
            name="fk_agent_job_run",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'timed_out', 'cancelled', 'unavailable')",
            name="ck_agent_job_status",
        ),
    )
    op.create_index("ix_agent_jobs_run", "agent_jobs", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_jobs_run", table_name="agent_jobs")
    op.drop_table("agent_jobs")
