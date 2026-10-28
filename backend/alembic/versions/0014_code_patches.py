"""patch-only coding agent: code patches, patch files, accepted workspace files (additive)

Revision ID: 0014_code_patches
Revises: 0013_plan_manual_dependencies
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_code_patches"
down_revision: str | None = "0013_plan_manual_dependencies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Target of the composite foreign key from code_patches (trivially unique: id is the key).
    with op.batch_alter_table("plan_tasks") as batch:
        batch.create_unique_constraint("uq_plan_task_id_project", ["id", "project_id"])

    op.create_table(
        "code_patches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("notes", sa.JSON(), nullable=False),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("test_suggestions", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("blocking_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(100), nullable=True),
        sa.Column("review_reason", sa.String(500), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_patch_plan",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["plan_tasks.id", "plan_tasks.project_id"],
            ondelete="CASCADE",
            name="fk_patch_task",
        ),
        sa.UniqueConstraint("task_id", "version", name="uq_patch_task_version"),
        sa.UniqueConstraint("id", "project_id", name="uq_patch_id_project"),
        sa.CheckConstraint("status IN ('proposed', 'accepted', 'rejected')", name="ck_patch_status"),
        sa.CheckConstraint("version >= 1", name="ck_patch_version"),
    )
    op.create_index("ix_patches_project_task", "code_patches", ["project_id", "task_key"])

    op.create_table(
        "patch_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("patch_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("change", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("previous_content_hash", sa.String(64), nullable=True),
        sa.Column("previous_content", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["patch_id", "project_id"],
            ["code_patches.id", "code_patches.project_id"],
            ondelete="CASCADE",
            name="fk_patch_file_patch",
        ),
        sa.UniqueConstraint("patch_id", "path", name="uq_patch_file_path"),
        sa.CheckConstraint("change IN ('create', 'modify', 'delete')", name="ck_patch_file_change"),
    )
    op.create_index("ix_patch_files_patch", "patch_files", ["patch_id"])

    op.create_table(
        "workspace_files",
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path", sa.String(300), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_patch_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_patch_id", "project_id"],
            ["code_patches.id", "code_patches.project_id"],
            ondelete="CASCADE",
            name="fk_workspace_file_patch",
        ),
    )

    op.create_table(
        "technology_confirmations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(20), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_tech_confirmation_plan",
        ),
    )
    op.create_index(
        "ix_tech_confirmations_task", "technology_confirmations", ["plan_id", "task_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_tech_confirmations_task", table_name="technology_confirmations")
    op.drop_table("technology_confirmations")
    op.drop_table("workspace_files")
    op.drop_index("ix_patch_files_patch", table_name="patch_files")
    op.drop_table("patch_files")
    op.drop_index("ix_patches_project_task", table_name="code_patches")
    op.drop_table("code_patches")
    with op.batch_alter_table("plan_tasks") as batch:
        batch.drop_constraint("uq_plan_task_id_project", type_="unique")
