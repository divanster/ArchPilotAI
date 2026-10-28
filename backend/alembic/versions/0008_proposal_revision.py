"""add architecture_proposals revision reference (nullable; existing drafts are unaffected)

Revision ID: 0008_proposal_revision
Revises: 0007_proposal_lint
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_proposal_revision"
down_revision: str | None = "0007_proposal_lint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("architecture_proposals") as batch:
        batch.add_column(sa.Column("revised_from_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("revised_from_version", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("revised_from_findings", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_architecture_revised_from",
            "architecture_proposals",
            ["revised_from_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("architecture_proposals") as batch:
        batch.drop_constraint("fk_architecture_revised_from", type_="foreignkey")
        batch.drop_column("revised_from_findings")
        batch.drop_column("revised_from_version")
        batch.drop_column("revised_from_id")
