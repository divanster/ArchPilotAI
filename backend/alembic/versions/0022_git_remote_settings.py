"""git remote settings: one external Git repository per project (additive, no secrets)

Revision ID: 0022_git_remote_settings
Revises: 0021_agent_run_deployment
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_git_remote_settings"
down_revision: str | None = "0021_agent_run_deployment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "git_remote_settings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("remote_url", sa.String(500), nullable=False),
        sa.Column("clone_folder", sa.String(200), nullable=False),
        sa.Column("base_branch", sa.String(100), nullable=False),
        sa.Column("branch_prefix", sa.String(50), nullable=False),
        sa.Column("author_name", sa.String(100), nullable=False),
        sa.Column("author_email", sa.String(200), nullable=False),
        sa.Column("auth_method", sa.String(20), nullable=False),
        sa.Column("pat_ref", sa.String(300), nullable=True),
        sa.Column("pat_provider", sa.String(20), nullable=True),
        sa.Column("pat_last4", sa.String(4), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("project_id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("clone_folder"),
        sa.CheckConstraint(
            "provider IN ('github', 'github_enterprise', 'azure_devops', 'gitlab', 'generic')",
            name="ck_git_remote_provider",
        ),
        sa.CheckConstraint(
            "auth_method IN ('credential_manager', 'ssh', 'pat')", name="ck_git_remote_auth"
        ),
    )


def downgrade() -> None:
    op.drop_table("git_remote_settings")
