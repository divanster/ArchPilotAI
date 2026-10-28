"""add architecture_proposals.lint_findings (nullable; existing drafts keep NULL = not linted)

Revision ID: 0007_proposal_lint
Revises: 0006_chunk_embeddings
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_proposal_lint"
down_revision: str | None = "0006_chunk_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("architecture_proposals", sa.Column("lint_findings", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("architecture_proposals", "lint_findings")
