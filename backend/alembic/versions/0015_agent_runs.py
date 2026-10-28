"""agent loop with static feedback: runs, steps, draft files (additive)

Revision ID: 0015_agent_runs
Revises: 0014_code_patches
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_agent_runs"
down_revision: str | None = "0014_code_patches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("stop_reason", sa.String(30), nullable=True),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("blocking_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("max_steps", sa.Integer(), nullable=False),
        sa.Column("token_budget", sa.Integer(), nullable=False),
        sa.Column("time_limit_seconds", sa.Float(), nullable=False),
        sa.Column("steps_taken", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("summary", sa.String(1000), nullable=True),
        sa.Column("patch_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_agent_run_plan",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["plan_tasks.id", "plan_tasks.project_id"],
            ondelete="CASCADE",
            name="fk_agent_run_task",
        ),
        sa.ForeignKeyConstraint(
            ["patch_id", "project_id"],
            ["code_patches.id", "code_patches.project_id"],
            ondelete="CASCADE",
            name="fk_agent_run_patch",
        ),
        sa.UniqueConstraint("id", "project_id", name="uq_agent_run_id_project"),
        sa.CheckConstraint(
            "status IN ('running', 'finished', 'stopped', 'failed')", name="ck_agent_run_status"
        ),
    )
    op.create_index("ix_agent_runs_project_task", "agent_runs", ["project_id", "task_key"])

    op.create_table(
        "agent_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("step_no", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("path", sa.String(300), nullable=True),
        sa.Column("args", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("result_summary", sa.String(300), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            ondelete="CASCADE",
            name="fk_agent_step_run",
        ),
        sa.UniqueConstraint("run_id", "step_no", name="uq_agent_step_no"),
        sa.CheckConstraint("status IN ('ok', 'error')", name="ck_agent_step_status"),
    )

    op.create_table(
        "agent_draft_files",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("path", sa.String(300), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("base_hash", sa.String(64), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            ondelete="CASCADE",
            name="fk_agent_draft_run",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_draft_files")
    op.drop_table("agent_steps")
    op.drop_index("ix_agent_runs_project_task", table_name="agent_runs")
    op.drop_table("agent_runs")
