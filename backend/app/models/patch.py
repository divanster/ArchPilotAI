import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.project import _utcnow


class CodePatch(Base):
    """A model-proposed set of file changes for one plan task. Nothing is ever executed.

    Only `status`, `reviewed_by`, `review_reason` and `reviewed_at` change after creation, and only
    once (proposed -> accepted | rejected). Its files are never edited.
    """

    __tablename__ = "code_patches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_patch_plan",
        ),
        ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["plan_tasks.id", "plan_tasks.project_id"],
            ondelete="CASCADE",
            name="fk_patch_task",
        ),
        UniqueConstraint("task_id", "version", name="uq_patch_task_version"),
        # Target of the composite foreign keys from patch_files and workspace_files.
        UniqueConstraint("id", "project_id", name="uq_patch_id_project"),
        CheckConstraint("status IN ('proposed', 'accepted', 'rejected')", name="ck_patch_status"),
        CheckConstraint("version >= 1", name="ck_patch_version"),
        Index("ix_patches_project_task", "project_id", "task_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_key: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    assumptions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    test_suggestions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Visible warnings from generation (dependencies without an accepted patch, cut context, ...).
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    blocking_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PatchFile(Base):
    __tablename__ = "patch_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["patch_id", "project_id"],
            ["code_patches.id", "code_patches.project_id"],
            ondelete="CASCADE",
            name="fk_patch_file_patch",
        ),
        UniqueConstraint("patch_id", "path", name="uq_patch_file_path"),
        CheckConstraint("change IN ('create', 'modify', 'delete')", name="ck_patch_file_change"),
        Index("ix_patch_files_patch", "patch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    patch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    change: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # SHA-256 of the accepted file this change was made against; null for "create".
    previous_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # That file's text, kept so the diff of a reviewed patch can still be shown.
    previous_content: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkspaceFile(Base):
    """The accepted version of one generated file. The workspace is exactly this set of rows."""

    __tablename__ = "workspace_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_patch_id", "project_id"],
            ["code_patches.id", "code_patches.project_id"],
            ondelete="CASCADE",
            name="fk_workspace_file_patch",
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(String(300), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_patch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class TechnologyConfirmation(Base):
    """A person's recorded confirmation of an AI-recommended technology for one task."""

    __tablename__ = "technology_confirmations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_tech_confirmation_plan",
        ),
        Index("ix_tech_confirmations_task", "plan_id", "task_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_key: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def as_json(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "name": self.name,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }
