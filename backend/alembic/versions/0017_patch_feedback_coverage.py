"""reviewer feedback version and coverage report on patches (additive)

Revision ID: 0017_patch_feedback_coverage
Revises: 0016_project_decisions
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_patch_feedback_coverage"
down_revision: str | None = "0016_project_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("code_patches", sa.Column("based_on_version", sa.Integer(), nullable=True))
    op.add_column("code_patches", sa.Column("coverage", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("code_patches") as batch:
        batch.drop_column("coverage")
        batch.drop_column("based_on_version")
