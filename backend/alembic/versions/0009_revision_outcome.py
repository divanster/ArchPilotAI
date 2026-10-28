"""add architecture_proposals.revised_outcome (nullable; existing drafts are unaffected)

Revision ID: 0009_revision_outcome
Revises: 0008_proposal_revision
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_revision_outcome"
down_revision: str | None = "0008_proposal_revision"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("architecture_proposals") as batch:
        batch.add_column(sa.Column("revised_outcome", sa.JSON(none_as_null=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("architecture_proposals") as batch:
        batch.drop_column("revised_outcome")
