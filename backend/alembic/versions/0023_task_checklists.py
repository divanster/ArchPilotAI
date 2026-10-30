"""task checklists: ordered coding steps of a plan task, and step runs (additive)

Revision ID: 0023_task_checklists
Revises: 0022_git_remote_settings
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023_task_checklists"
down_revision: str | None = "0022_git_remote_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_checklists",
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
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("manual_verification", sa.JSON(), nullable=False),
        sa.Column("proposed_by", sa.String(100), nullable=False),
        sa.Column("proposal_reason", sa.String(1000), nullable=True),
        sa.Column("decided_by", sa.String(100), nullable=True),
        sa.Column("decision_reason", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_checklist_plan",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["plan_tasks.id", "plan_tasks.project_id"],
            ondelete="CASCADE",
            name="fk_checklist_task",
        ),
        sa.UniqueConstraint("task_id", "version", name="uq_checklist_task_version"),
        sa.UniqueConstraint("id", "project_id", name="uq_checklist_id_project"),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name="ck_checklist_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_checklist_version"),
    )
    op.create_index(
        "ix_task_checklists_project_task", "task_checklists", ["project_id", "task_id"]
    )
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("checklist_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("checklist_step_key", sa.String(10), nullable=True))
        batch.add_column(sa.Column("resumed_from_run_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_agent_run_checklist",
            "task_checklists",
            ["checklist_id", "project_id"],
            ["id", "project_id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_agent_run_resumed_from",
            "agent_runs",
            ["resumed_from_run_id", "project_id"],
            ["id", "project_id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_constraint("fk_agent_run_resumed_from", type_="foreignkey")
        batch.drop_constraint("fk_agent_run_checklist", type_="foreignkey")
        batch.drop_column("resumed_from_run_id")
        batch.drop_column("checklist_step_key")
        batch.drop_column("checklist_id")
    op.drop_index("ix_task_checklists_project_task", table_name="task_checklists")
    op.drop_table("task_checklists")
