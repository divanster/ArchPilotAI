"""agent hook settings: per-project switches of the agent hooks (additive)

Revision ID: 0019_agent_hook_settings
Revises: 0018_agent_jobs
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019_agent_hook_settings"
down_revision: str | None = "0018_agent_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_hook_settings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("hook", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "hook"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("agent_hook_settings")
