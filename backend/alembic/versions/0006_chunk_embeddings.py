"""create chunk_embeddings table (portable JSON vectors; no pgvector)

Revision ID: 0006_chunk_embeddings
Revises: 0005_document_chunks
Create Date: 2026-10-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_chunk_embeddings"
down_revision: str | None = "0005_document_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("document_chunks") as batch:
        batch.create_unique_constraint("uq_document_chunks_id_project", ["id", "project_id"])
    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("actual_model", sa.String(100), nullable=True),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("embedder_version", sa.String(50), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id", "project_id"],
            ["document_chunks.id", "document_chunks.project_id"],
            name="fk_chunk_embeddings_chunk_project",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "chunk_id",
            "provider",
            "model",
            "dimension",
            "embedder_version",
            name="uq_chunk_embeddings_space",
        ),
        sa.CheckConstraint("dimension > 0", name="ck_chunk_embeddings_dimension"),
    )
    op.create_index(
        "ix_chunk_embeddings_space",
        "chunk_embeddings",
        ["project_id", "provider", "model", "dimension", "embedder_version"],
    )


def downgrade() -> None:
    op.drop_table("chunk_embeddings")
    with op.batch_alter_table("document_chunks") as batch:
        batch.drop_constraint("uq_document_chunks_id_project", type_="unique")
