"""agent run deployment: the chat deployment chosen for a run (additive, nullable)

Revision ID: 0021_agent_run_deployment
Revises: 0020_agent_run_hooks
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_agent_run_deployment"
down_revision: str | None = "0020_agent_run_hooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("deployment", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "deployment")
