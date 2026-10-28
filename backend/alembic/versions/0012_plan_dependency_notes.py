"""plan dependency notes and plan-level warnings (additive; existing plans get empty values)

Revision ID: 0012_plan_dependency_notes
Revises: 0011_implementation_plans
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_plan_dependency_notes"
down_revision: str | None = "0011_implementation_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMPTY_LIST = sa.text("'[]'")


def upgrade() -> None:
    with op.batch_alter_table("implementation_plans") as batch:
        batch.add_column(
            sa.Column("not_for_client", sa.JSON(), nullable=False, server_default=EMPTY_LIST)
        )
        batch.add_column(
            sa.Column("dependency_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("unmapped_node_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("warnings", sa.JSON(), nullable=False, server_default=EMPTY_LIST))
    with op.batch_alter_table("plan_tasks") as batch:
        batch.add_column(sa.Column("notes", sa.JSON(), nullable=False, server_default=EMPTY_LIST))
        batch.add_column(sa.Column("related", sa.JSON(), nullable=False, server_default=EMPTY_LIST))


def downgrade() -> None:
    with op.batch_alter_table("plan_tasks") as batch:
        batch.drop_column("related")
        batch.drop_column("notes")
    with op.batch_alter_table("implementation_plans") as batch:
        batch.drop_column("warnings")
        batch.drop_column("unmapped_node_count")
        batch.drop_column("dependency_count")
        batch.drop_column("not_for_client")
