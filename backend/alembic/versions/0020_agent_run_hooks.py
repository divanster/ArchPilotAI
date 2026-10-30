"""agent run hooks: the hooks active when a run started (additive, nullable)

Revision ID: 0020_agent_run_hooks
Revises: 0019_agent_hook_settings
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_agent_run_hooks"
down_revision: str | None = "0019_agent_hook_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("hooks", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "hooks")
