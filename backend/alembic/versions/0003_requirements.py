"""create requirements and extraction_runs tables

Revision ID: 0003_requirements
Revises: 0002_documents
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_requirements"
down_revision: str | None = "0002_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.create_unique_constraint("uq_documents_id_project", ["id", "project_id"])

    op.create_table(
        "extraction_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "project_id"],
            ["documents.id", "documents.project_id"],
            name="fk_extraction_runs_document_project",
        ),
        sa.CheckConstraint("status IN ('succeeded', 'failed')", name="ck_extraction_runs_status"),
    )
    op.create_index("ix_extraction_runs_document", "extraction_runs", ["document_id"])

    op.create_table(
        "requirements",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "extraction_run_id",
            sa.Uuid(),
            sa.ForeignKey("extraction_runs.id"),
            nullable=False,
        ),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_quote", sa.Text(), nullable=False),
        sa.Column("source_start", sa.Integer(), nullable=False),
        sa.Column("source_end", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "project_id"],
            ["documents.id", "documents.project_id"],
            name="fk_requirements_document_project",
        ),
        sa.CheckConstraint(
            "type IN ('business', 'functional', 'non_functional', 'constraint')",
            name="ck_requirements_type",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'confirmed', 'rejected')", name="ck_requirements_status"
        ),
        sa.CheckConstraint(
            "source_start >= 0 AND source_end > source_start "
            "AND source_end - source_start = length(source_quote)",
            name="ck_requirements_source_span",
        ),
    )
    op.create_index("ix_requirements_project_status", "requirements", ["project_id", "status"])
    op.create_index("ix_requirements_document", "requirements", ["document_id"])


def downgrade() -> None:
    op.drop_table("requirements")
    op.drop_table("extraction_runs")
    with op.batch_alter_table("documents") as batch:
        batch.drop_constraint("uq_documents_id_project", type_="unique")
